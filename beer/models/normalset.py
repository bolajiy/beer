
'''Set of Normal densities with prior over the mean and
covariance matrix.

'''

import abc
from collections import namedtuple
import math
import torch

from .parameters import JointConjugateBayesianParameters
from .parameters import BayesianParameterSet
from .modelset import ModelSet
from .normal import Normal
from .normal import _full_cov
from .normal import UnknownCovarianceType
from ..dists import IsotropicNormalGamma
from ..dists import IsotropicNormalGammaStdParams
from ..dists import NormalGamma
from ..dists import NormalGammaStdParams
from ..dists import NormalWishart
from ..dists import NormalWishartStdParams


__all__ = ['NormalSet']


########################################################################
# Helper to build the default parameters.

def _default_fullcov_param(mean, cov, prior_strength, tensorconf):
    cov = _full_cov(cov, mean.shape[-1], tensorconf)
    scale = torch.tensor(prior_strength, **tensorconf)
    dof = torch.tensor(prior_strength + len(mean) - 1, **tensorconf)
    scale_matrix = cov.inverse() / dof
    params = NormalWishartStdParams(mean, scale, scale_matrix, dof)
    prior = NormalWishart(params)
    params = NormalWishartStdParams(mean, scale, scale_matrix, dof)
    posterior = NormalWishart(params)
    return JointConjugateBayesianParameters(prior, posterior)

def _default_diagcov_param(mean, cov, size, prior_strength, noise_std, 
                           tensorconf):
    cov = _full_cov(cov, mean.shape[-1], tensorconf)
    means = mean.repeat(size, 1)
    noise = torch.randn(size, len(mean), **tensorconf) * noise_std
    scale = torch.tensor(prior_strength, **tensorconf).repeat(size, 1)
    shape = torch.tensor(prior_strength, **tensorconf).repeat(size, 1)
    rates = prior_strength * cov.diag().repeat(size, 1)
    params = NormalGammaStdParams(means, scale, shape, rates)
    prior = NormalGamma(params)
    params = NormalGammaStdParams(means + noise, scale, shape, rates)
    posterior = NormalGamma(params)
    return JointConjugateBayesianParameters(prior, posterior)

def _default_isocov_param(mean, cov, size, prior_strength, noise_std, 
                          tensorconf):
    cov = _full_cov(cov, mean.shape[-1], tensorconf)
    variance = cov.diag().max()
    means = mean.repeat(size, 1)
    noise = torch.randn(size, len(mean), **tensorconf) * noise_std
    scale = torch.tensor(prior_strength, **tensorconf).repeat(size, 1)
    shape = torch.tensor(prior_strength, **tensorconf).repeat(size, 1)
    rate =  prior_strength * variance.repeat(size, 1)
    params = IsotropicNormalGammaStdParams(means, scale, shape, rate)
    prior = IsotropicNormalGamma(params)
    params = IsotropicNormalGammaStdParams(means + noise, scale, shape, rate)
    posterior = IsotropicNormalGamma(params)
    return JointConjugateBayesianParameters(prior, posterior)

_default_param = {
    'full': _default_fullcov_param,
    'diagonal': _default_diagcov_param,
    'isotropic': _default_isocov_param,
}

########################################################################


class NormalSet(ModelSet):
    '''Set of Normal models.'''

    @classmethod
    def create(cls, mean, cov, size, prior_strength=1, noise_std=1.,
               cov_type='full', shared_cov=False):
        if shared_cov:
            import warnings
            warnings.warn('The "NormalSet" with shared covariance is ' \
                          'supported anymore. The argument will be ignored.',
                          DeprecationWarning, stacklevel=2)
    
        if cov_type not in cov_type:
            raise UnknownCovarianceType('Unknown covariance type: ' \
                                        f'"{cov_type}"')

        tensorconf = {'dtype': mean.dtype, 'device': mean.device, 
                      'requires_grad': False}
        mean = mean.detach()
        cov = cov.detach()
        makeparam = _default_param[cov_type]
        return cls(makeparam(mean, cov, size, prior_strength, noise_std,
                             tensorconf))

    def __init__(self, means_precisions):
        super().__init__()
        self.means_precisions = means_precisions

    ####################################################################
    # Model interface.

    def sufficient_statistics(self, data):
        return self.means_precisions.likelihood_fn.sufficient_statistics(data)

    def mean_field_factorization(self):
        return [[self.means_precisions]]

    def expected_log_likelihood(self, stats):
        nparams = self.means_precisions.natural_form()
        return self.means_precisions.likelihood_fn(nparams, stats)

    def accumulate(self, stats, weights):
        w_stats = weights.t() @ stats
        return {self.means_precisions: w_stats}

    ####################################################################
    # ModelSet interface.

    def __len__(self):
        return len(self.means_precisions)

    def __getitem__(self, key): 
        if isinstance(key, slice):
            self.__class__(self.means_precisions[key])
        return Normal(self.means_precisions[key])
