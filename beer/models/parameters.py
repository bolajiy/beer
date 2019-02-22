import abc
import uuid
import typing
import torch
from ..dists import ExponentialFamily


__all__ = ['BayesianParameter', 'BayesianParameterSet', 
           'ConjugateBayesianParameter']

# Empty object for pretty representation of the Subspace Bayesian 
# paramters.
class _UNSPECIFIED_POSTERIOR_CLASS:
    def __repr__(self):
        return '<unspecified>'
_UNSPECIFIED_POSTERIOR = _UNSPECIFIED_POSTERIOR_CLASS()

class BayesianParameter(torch.nn.Module, metaclass=abc.ABCMeta):
    '''Base class for a Bayesian Parameter (i.e. a parameter with a prior
     and a posterior distribution.
     
    '''

    def __init__(self, init_stats, prior, posterior=_UNSPECIFIED_POSTERIOR):
        super().__init__()
        self.prior = prior
        self.posterior = posterior
        self.register_buffer('_stats', init_stats.clone().detach())
        self._callbacks = set()
        self._uuid = uuid.uuid4()

    # We override the default repr provided by torch's modules to make
    # the BEER model tree clearer.
    def __repr__(self):
        class_name = self.__class__.__qualname__
        prior_name = self.prior.__class__.__qualname__
        if self.posterior is not _UNSPECIFIED_POSTERIOR:
            post_name = self.posterior.__class__.__qualname__
        else:
            post_name = _UNSPECIFIED_POSTERIOR
        return f'{class_name}(prior={prior_name}, posterior={post_name})'

    def __hash__(self):
        return hash(self._uuid)

    def __eq__(self, other):
        if other.__class__ is other.__class__:
            return hash(self) == hash(other)
        raise NotImplementedError

    @property
    def stats(self):
        'Accumulated sufficient statistics.'
        return self._stats

    def dispatch(self):
        'Notify the observers the parameter has changed.'
        for callback in self._callbacks:
            callback()

    def register_callback(self, callback):
        '''Register a callback function that will be called every time
        the parameters if updated. The function takes no argument.

        '''
        self._callbacks.add(callback)

    def zero_stats(self):
        'Reset the accumulated statistics.'
        self._stats.zero_()

    def store_stats(self, acc_stats):
        '''Store the accumulated statistics.

        Args:
            acc_stats (``torch.Tensor[dim]``): Accumulated statistics
                of the parameter.

        '''
        # To avoid memory issue, we make sure that the stored
        # statistics are not differentiable (therefore they do not keep
        # track of the computation graph).
        if acc_stats.requires_grad:
            self._stats = acc_stats.clone().detach()
        else:
            self._stats = acc_stats

    ####################################################################
    # TODO: to be removed

    def expected_natural_parameters(self):
        import warnings
        warnings.warn('The "expected_natural_parameters" method is ' \
                      'deprecated. Use the "natural_form" method instead.', 
                      DeprecationWarning, stacklevel=2)
        return self.natural_form()
    
    def expected_value(self):
        import warnings
        warnings.warn('The "expected_value" method is ' \
                      'deprecated. Use the "value" method instead.', 
                      DeprecationWarning, stacklevel=2)
        return self.value()

    ####################################################################
    # Interface to be implemented by other subclasses.

    @abc.abstractmethod
    def sufficient_statistics(self, data):
        '''Extract the sufficient statistics of the parameter from the 
        data.
        '''
        pass

    @abc.abstractmethod
    def value(self):
        '''Value of the parameter w.r.t. the posterior
        distribution of the parameter. Note that, according to the
        concrete class of the parameter, the "type" of the 
        returned value depends on the concrete paramter class. For 
        instance, it can be the expectation of the natural form of the 
        parameter w.r.t. the posterior distribution or a stochastic
        sampled from the posterior distribution. 

        Returns:
            ``torch.Tensor`` or eventually a tuple of ``torch.Tensor``.
        '''
        pass

    @abc.abstractmethod
    def natural_form(self):
        '''Natural form of the parameter. Note that, according to the
        concrete class of the parameter, the "type" of the 
        returned value may vary. For instance, it can be the expectation
        or a sampled drawn from the posterior distribution.

        Returns:
            ``torch.Tensor``.
        '''
        pass


class BayesianParameterSet(torch.nn.ModuleList):
    '''Set of Bayesian parameters.'''

    def expected_natural_parameters(self):
        import warnings
        warnings.warn('The "expected_natural_parameters" method is ' \
                      'deprecated. Use the "natural_form" method instead.',
                       DeprecationWarning, stacklevel=2)
        return self.natural_form()
    
    def natural_form(self):
        '''Natural form of the parameters.

        Returns:
            ``torch.Tensor[k,dim]`` where k is the number of elements of
                the set.
        '''
        return torch.cat([
            param.natural_form().view(1, -1)
            for param in self], 
        dim=0)


class ConjugateBayesianParameter(BayesianParameter):
    '''Parameter for model having likelihood conjugate to its prior.

    Note:
        The type of the prior is the same as the posterior. 
    
    '''

    def __init__(self, prior, posterior):
        init_stats = torch.zeros_like(prior.natural_parameters())
        self._conjugate = prior.conjugate()
        super().__init__(init_stats, prior, posterior)
        
    @property
    def sufficient_statistics_dim(self):
        return self._conjugate.sufficient_statistics_dim
    
    def sufficient_statistics(self, data):
        return self._conjugate.sufficient_statistics(data)

    def value(self):
        return self.posterior.expected_value()

    def natural_form(self):
        return self.posterior.expected_sufficient_statistics()
    
    def natural_grad_update(self, lrate):
        prior_nparams = self.prior.natural_parameters()
        posterior_nparams = self.posterior.natural_parameters()
        natural_grad = prior_nparams + self._stats - posterior_nparams
        new_nparams = posterior_nparams + lrate * natural_grad
        self.posterior.update_from_natural_parameters(new_nparams)
        self.dispatch()
