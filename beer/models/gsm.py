import abc
import math
import torch

from .bayesmodel import BayesianModel
from .parameters import BayesianParameter, ConstantParameter
from ..priors import NormalFullCovariancePrior
from ..priors import MatrixNormalPrior
from ..utils import make_symposdef


def _trace_array(matrices):
    'Compute trace of a set of matrices.'
    dim = len(matrices[0])
    idxs = tuple(range(dim))
    return matrices[:, idxs, idxs].sum(dim=-1)


class GeneralizedSubspaceModel(BayesianModel):
    '''Bayesian Generalized Subspace Model.

    Attributes:
        weights: weights matrix parameter.
        precision: precision parameter.

    '''

    @staticmethod
    def create(llh_func, mean_subspace, global_mean, noise_std=0.,
               prior_strength=1., hessian_type='full'):
        '''Create a Bayesian Generalized Subspace Model.

        Args:
            llh_func: (:any:`LikelihoodFunction`): The type of model
                of the subpsace.
            mean_subspace (``torch.Tensor[K,D]``): Mean of the prior
                over the bases of the subspace. K is the dimension of
                the subspace and D the dimension of the parameter
                space.
            global_mean (``torch.Tensor[D]``): Mean  of the prior
                over the global mean in the parameter space.
            noise_std (float): Standard deviation of the noise for the
                random initialization of the subspace.
            prior_strength (float): Strength of the prior over the
                bases of the subspace and the global mean.
            hessian_type (string): Type of approximation. Possible
                choices are "full" (no approximation), "diagonal"
                or "scalar".

        '''
        args = (
            llh_func, mean_subspace, global_mean, noise_std,
            prior_strength
        )
        if hessian_type == 'full':
            return GeneralizedSubspaceModelFull.create(*args)
        elif hessian_type == 'diagonal':
            return GeneralizedSubspaceModelDiagonal.create(*args)
        elif hessian_type == 'scalar':
            return GeneralizedSubspaceModelScalar.create(*args)
        else:
            raise ValueError(f'Unknown hessian type: "{hessian_type}"')


    def __init__(self, llh_func, subspace_prior, subspace_posterior,
                 mean_prior, mean_posterior, latent_prior):
        super().__init__()
        self.subspace = BayesianParameter(subspace_prior, subspace_posterior)
        self.mean = BayesianParameter(mean_prior, mean_posterior)
        self.latent_prior = ConstantParameter(latent_prior)
        self.llh_func = llh_func

    def _create_latent_posteriors(self, n):
        std_params = self.latent_prior.value.to_std_parameters()
        return [NormalFullCovariancePrior(*std_params) for i in range(n)]

    def _extract_moments(self):
        m, mm = self.mean.posterior.moments()
        W, WW = self.subspace.posterior.moments()
        return m, mm, W, WW

    # Quadratic approximation of the likelihood functions given the
    # maximized latent posteriors.
    @abc.abstractmethod
    def _quad_approx(self, cache):
        pass

    def _latent_kl_div(self, l_posts):
        kl_div_func = self.latent_prior.value.kl_div
        nparams_0 = self.latent_prior.value.natural_parameters
        dtype, device = nparams_0.dtype, nparams_0.device
        kl_divs = [kl_div_func(l_post, self.latent_prior.value)
                   for l_post in l_posts]
        return torch.tensor(kl_divs, dtype=dtype, device=device)

    @abc.abstractmethod
    def latent_posteriors(self, data, cache=False):
        '''Compute the latent posteriors for the given data.

        Args:
            data (``torch.Tensor``: Accumulated statistics of the
                likelihood functions and the the number of data points
                per function.
            cache (boolean): If true, cache the intermediate
                computation into a dictionary and return it.

        Returns:
            list of :any:`NormalFullCovariancePrior`
            (dict: intermediate computation)

        '''
        pass

    ####################################################################
    # BayesianModel interface.
    ####################################################################

    def mean_field_factorization(self):
        return [[self.mean, self.subspace]]

    @staticmethod
    def sufficient_statistics(data):
        return data

    def expected_log_likelihood(self, stats):
        l_posts, cache = self.latent_posteriors(stats, cache=True)
        approx_llh, cache = self._quad_approx(cache)
        self.cache.update(cache)
        return (approx_llh - self._latent_kl_div(l_posts))

    @abc.abstractmethod
    def accumulate(self, stats):
        pass


class GeneralizedSubspaceModelFull(GeneralizedSubspaceModel):

    @classmethod
    def create(cls, llh_func, mean_subspace, global_mean, noise_std=0.,
               prior_strength=1.):
        dim_s = len(mean_subspace)
        dim_o = len(global_mean)
        dtype, device = mean_subspace.dtype, mean_subspace.device
        with torch.no_grad():
            # Subspace prior/posterior.
            mean_subspace.requires_grad = False
            global_mean.requires_grad = False
            U = torch.eye(dim_s, dtype=dtype, device=device)
            r_M = mean_subspace + noise_std * torch.randn(dim_s,
                                                          dim_o,
                                                          dtype=dtype,
                                                          device=device)
            subspace_prior = MatrixNormalPrior(mean_subspace, U)
            subspace_posterior = MatrixNormalPrior(r_M, U)

            # Global mean prior/posterior.
            S = prior_strength * torch.eye(dim_o, dim_o, dtype=dtype,
                                           device=device)
            mean_prior = NormalFullCovariancePrior(global_mean, S)
            mean_posterior = NormalFullCovariancePrior(global_mean, S)

            # Latent prior.
            latent_mean = torch.zeros(dim_s, dtype=dtype, device=device)
            latent_cov = torch.eye(dim_s, dim_s, dtype=dtype, device=device)
            latent_prior = NormalFullCovariancePrior(latent_mean, latent_cov)

            return cls(llh_func, subspace_prior, subspace_posterior,
                       mean_prior, mean_posterior, latent_prior)

    def _quad_approx(self, cache):
        opt, hessians = cache['opt'], cache['hessians']
        m, mm = cache['m'], cache['mm']
        W, WW, U = cache['W'], cache['WW'], cache['U']
        WHW = cache['WHW']
        H, HH = cache['H'], cache['HH']
        stats, counts = cache['stats'], cache['counts']

        mHm = torch.sum(hessians.reshape(len(hessians), -1) * mm, dim=-1)
        hW = H @ W
        Hm = hessians @ m
        mHWh = (hW * Hm).sum(dim=-1)
        oHm = torch.sum(opt * Hm, dim=-1)
        oH = (hessians * opt[:, None, :]).sum(dim=-1)
        oHWh = torch.sum(oH * hW, dim=-1)
        hWHWh = (HH * WHW).sum(dim=-1)

        opt_quad = opt[:, :, None] * opt[:, None, :]
        opt_quad = opt_quad.reshape(len(opt), -1)
        rs_hessians = hessians.reshape(len(opt), -1)
        quad_opt = torch.sum(rs_hessians * opt_quad, dim=-1)

        new_cache = {**cache,
            'hW': hW,
        }

        return  self.llh_func(opt, stats, counts) \
                     + .5 * (hWHWh + mHm + quad_opt  \
                            + 2 * (mHWh - oHm - oHWh)), new_cache

    def latent_posteriors(self, data, cache=False):
        '''Compute the latent posteriors for the given data.

        Args:
            data (``torch.Tensor``: Accumulated statistics of the
                likelihood functions and the the number of data points
                per function.
            cache (boolean): If true, cache the intermediate
                computation into a dictionary and return it.

        Returns:
            list of :any:`NormalFullCovariancePrior`
            (dict: intermediate computation)

        '''
        stats = data[:, :-1]
        counts = data[:, -1]
        l_posts = self._create_latent_posteriors(len(data))
        m, mm, W, WW = self._extract_moments()

        opt = self.llh_func.argmax(stats, counts)
        hessians = self.llh_func.hessian(opt, stats, counts, mode='full')

        U = self.subspace.posterior.cov
        tr_hessians = _trace_array(hessians)
        WHW = tr_hessians[:, None, None] * U + W @ hessians @ W.t()
        WHW = WHW.reshape(len(hessians), -1)
        m_opt = (opt - m[None])
        Hm_o = (hessians * m_opt[:, None, :]).sum(dim=-1)
        acc_stats =  torch.cat([
            -Hm_o @ W.t(),
            .5 * WHW
        ], dim=-1)

        nparams_0 = self.latent_prior.value.natural_parameters
        for i, l_post in enumerate(l_posts):
            l_post.natural_parameters = nparams_0 + acc_stats[i]

        if not cache:
            return l_posts

        # TODO: possible optimization by doing a single for-loop.
        H = torch.cat([pdf.moments()[0][None, :]
                       for pdf in l_posts], dim=0)
        HH = torch.cat([pdf.moments()[1][None, :]
                        for pdf in l_posts], dim=0)
        cache = {
            'U': U,
            'opt': opt,
            'hessians': hessians,
            'tr_hessians': tr_hessians,
            'm': m,
            'mm': mm,
            'W': W,
            'WW': WW,
            'H': H,
            'HH': HH,
            'stats': stats,
            'counts': counts,
            'WHW': WHW,
        }
        return l_posts, cache

    ####################################################################
    # BayesianModel interface.
    ####################################################################

    def accumulate(self, stats):
        counts = stats[:, -1]
        hessians = self.cache['hessians']
        tr_hessians = self.cache['tr_hessians']
        hW = self.cache['hW']
        opt = self.cache['opt']
        m = self.cache['m']
        H = self.cache['H']
        HH = self.cache['HH']

        # Mean stats.
        HWh_o = torch.sum(hessians * (opt - hW)[:, None, :], dim=-1)
        #sum_hessians = make_symposdef(.5 * hessians.sum(dim=0))
        sum_hessians = .5 * hessians.sum(dim=0)
        mean_stats = torch.cat([
            -HWh_o.sum(dim=0),
            sum_hessians.reshape(-1)
        ], dim=-1)

        # Subspace stats.
        #idxs = tuple(range(hessians.shape[-1]))
        #isometric_params = hessians[:, idxs, idxs].max(dim=-1)[0][:, None]
        isometric_params = tr_hessians[:, None] / len(m)
        to_m = (opt - m) * isometric_params
        tHH = HH * isometric_params
        #tHH = make_symposdef(.5 * tHH.sum(dim=0).reshape(HH.shape[-1], -1))
        tHH = .5 * tHH.sum(dim=0).reshape(HH.shape[-1], -1)
        subspace_stats = torch.cat([
            -(H.t() @ to_m).reshape(-1),
            tHH.reshape(-1)
        ])

        return {
            self.mean: mean_stats,
            self.subspace: subspace_stats
        }


class GeneralizedSubspaceModelDiagonal(GeneralizedSubspaceModel):

    @classmethod
    def create(cls, llh_func, mean_subspace, global_mean, noise_std=0.,
               prior_strength=1.):
        dim_s = len(mean_subspace)
        dim_o = len(global_mean)
        dtype, device = mean_subspace.dtype, mean_subspace.device
        with torch.no_grad():
            # Subspace prior/posterior.
            mean_subspace.requires_grad = False
            global_mean.requires_grad = False
            U = torch.eye(dim_s, dtype=dtype, device=device)
            r_M = mean_subspace + noise_std * torch.randn(dim_s,
                                                          dim_o,
                                                          dtype=dtype,
                                                          device=device)
            subspace_prior = MatrixNormalPrior(mean_subspace, U)
            subspace_posterior = MatrixNormalPrior(r_M, U)

            # Global mean prior/posterior.
            S = prior_strength * torch.eye(dim_o, dim_o, dtype=dtype,
                                           device=device)
            mean_prior = NormalFullCovariancePrior(global_mean, S)
            mean_posterior = NormalFullCovariancePrior(global_mean, S)

            # Latent prior.
            latent_mean = torch.zeros(dim_s, dtype=dtype, device=device)
            latent_cov = torch.eye(dim_s, dim_s, dtype=dtype, device=device)
            latent_prior = NormalFullCovariancePrior(latent_mean, latent_cov)

            return cls(llh_func, subspace_prior, subspace_posterior,
                       mean_prior, mean_posterior, latent_prior)

    def _quad_approx(self, cache):
        opt, hessians = cache['opt'], cache['hessians']
        m, mm = cache['m'], cache['mm']
        W, WW, U = cache['W'], cache['WW'], cache['U']
        WHW = cache['WHW']
        H, HH = cache['H'], cache['HH']
        stats, counts = cache['stats'], cache['counts']

        mm_diag = torch.diag(mm.reshape(m.shape[0], m.shape[0]))
        mHm = torch.sum(hessians * mm_diag[None], dim=-1)
        hW = H @ W
        Hm = hessians * m
        mHWh = (hW * Hm).sum(dim=-1)
        oHm = torch.sum(opt * Hm, dim=-1)
        oH = hessians * opt
        oHWh = torch.sum(oH * hW, dim=-1)
        hWHWh = (HH * WHW).sum(dim=-1)
        quad_opt = torch.sum((hessians * opt) * opt, dim=-1)

        new_cache = {**cache,
            'hW': hW,
        }

        return  self.llh_func(opt, stats, counts) \
                     + .5 * (hWHWh + mHm + quad_opt  \
                            + 2 * (mHWh - oHm - oHWh)), new_cache

    def latent_posteriors(self, data, cache=False):
        stats = data[:, :-1]
        counts = data[:, -1]
        l_posts = self._create_latent_posteriors(len(data))
        m, mm, W, WW = self._extract_moments()

        opt = self.llh_func.argmax(stats, counts)
        hessians = self.llh_func.hessian(opt, stats, counts, mode='diagonal')

        U = self.subspace.posterior.cov
        tr_hessians = hessians.sum(dim=-1)
        HW = hessians[:, None, :] * W[None]
        WHW = tr_hessians[:, None, None] * U + HW @ W.t()
        WHW = WHW.reshape(len(hessians), -1)
        m_opt = (opt - m[None])
        Hm_o = hessians * m_opt
        acc_stats =  torch.cat([
            -Hm_o @ W.t(),
            .5 * WHW
        ], dim=-1)

        nparams_0 = self.latent_prior.value.natural_parameters
        for i, l_post in enumerate(l_posts):
            l_post.natural_parameters = nparams_0 + acc_stats[i]

        if not cache:
            return l_posts

        # TODO: possible optimization by doing a single for-loop.
        H = torch.cat([pdf.moments()[0][None, :]
                       for pdf in l_posts], dim=0)
        HH = torch.cat([pdf.moments()[1][None, :]
                        for pdf in l_posts], dim=0)
        cache = {
            'U': U,
            'opt': opt,
            'hessians': hessians,
            'tr_hessians': tr_hessians,
            'm': m,
            'mm': mm,
            'W': W,
            'WW': WW,
            'H': H,
            'HH': HH,
            'stats': stats,
            'counts': counts,
            'WHW': WHW,
        }
        return l_posts, cache

    ####################################################################
    # BayesianModel interface.
    ####################################################################

    def accumulate(self, stats):
        counts = stats[:, -1]
        hessians = self.cache['hessians']
        tr_hessians = self.cache['tr_hessians']
        hW = self.cache['hW']
        opt = self.cache['opt']
        m = self.cache['m']
        H = self.cache['H']
        HH = self.cache['HH']

        # Mean stats.
        HWh_o = hessians * (opt - hW)
        I = torch.eye(len(m), dtype=m.dtype, device=m.device)
        mean_stats = torch.cat([
            -HWh_o.sum(dim=0),
            .5 * (hessians[:, None, :] * I[None]).sum(dim=0).reshape(-1)
        ], dim=-1)

        # Subspace stats.
        #isometric_params = hessians.max(dim=-1)[0][:, None]
        isometric_params = tr_hessians[:, None] / len(m)
        to_m = (opt - m) * isometric_params
        tHH = HH * isometric_params
        subspace_stats = torch.cat([
            -(H.t() @ to_m).reshape(-1),
            .5 * tHH.sum(dim=0)
        ])

        return {
            self.mean: mean_stats,
            self.subspace: subspace_stats
        }


class GeneralizedSubspaceModelScalar(GeneralizedSubspaceModel):

    @classmethod
    def create(cls, llh_func, mean_subspace, global_mean, noise_std=0.,
               prior_strength=1.):
        dim_s = len(mean_subspace)
        dim_o = len(global_mean)
        dtype, device = mean_subspace.dtype, mean_subspace.device
        with torch.no_grad():
            # Subspace prior/posterior.
            mean_subspace.requires_grad = False
            global_mean.requires_grad = False
            U = torch.eye(dim_s, dtype=dtype, device=device)
            r_M = mean_subspace + noise_std * torch.randn(dim_s,
                                                          dim_o,
                                                          dtype=dtype,
                                                          device=device)
            subspace_prior = MatrixNormalPrior(mean_subspace, U)
            subspace_posterior = MatrixNormalPrior(r_M, U)

            # Global mean prior/posterior.
            S = prior_strength * torch.eye(dim_o, dim_o, dtype=dtype,
                                           device=device)
            mean_prior = NormalFullCovariancePrior(global_mean, S)
            mean_posterior = NormalFullCovariancePrior(global_mean, S)

            # Latent prior.
            latent_mean = torch.zeros(dim_s, dtype=dtype, device=device)
            latent_cov = torch.eye(dim_s, dim_s, dtype=dtype, device=device)
            latent_prior = NormalFullCovariancePrior(latent_mean, latent_cov)

            return cls(llh_func, subspace_prior, subspace_posterior,
                       mean_prior, mean_posterior, latent_prior)

    def _quad_approx(self, cache):
        opt, hessians = cache['opt'], cache['hessians']
        m, mm = cache['m'], cache['mm']
        W, WW, U = cache['W'], cache['WW'], cache['U']
        WHW = cache['WHW']
        H, HH = cache['H'], cache['HH']
        stats, counts = cache['stats'], cache['counts']

        mm_diag = torch.diag(mm.reshape(m.shape[0], m.shape[0]))
        mHm = torch.sum(hessians[:, None] * mm_diag[None], dim=-1)
        hW = H @ W
        Hm = hessians[:,None] * m
        mHWh = (hW * Hm).sum(dim=-1)
        oHm = torch.sum(opt * Hm, dim=-1)
        oH = hessians[:, None] * opt
        oHWh = torch.sum(oH * hW, dim=-1)
        hWHWh = (HH * WHW).sum(dim=-1)
        quad_opt = torch.sum((hessians[:, None] * opt) * opt, dim=-1)

        new_cache = {**cache,
            'hW': hW,
        }

        return  self.llh_func(opt, stats, counts) \
                     + .5 * (hWHWh + mHm + quad_opt  \
                            + 2 * (mHWh - oHm - oHWh)), new_cache

    def latent_posteriors(self, data, cache=False):
        stats = data[:, :-1]
        counts = data[:, -1]
        l_posts = self._create_latent_posteriors(len(data))
        m, mm, W, WW = self._extract_moments()

        opt = self.llh_func.argmax(stats, counts)
        hessians = self.llh_func.hessian(opt, stats, counts, mode='scalar')

        U = self.subspace.posterior.cov
        tr_hessians = hessians * len(m)
        HW = hessians[:, None, None] * W[None]
        WHW = tr_hessians[:, None, None] * U + HW @ W.t()
        WHW = WHW.reshape(len(hessians), -1)
        m_opt = (opt - m[None])
        Hm_o = hessians[:, None] * m_opt
        acc_stats =  torch.cat([
            -Hm_o @ W.t(),
            .5 * WHW
        ], dim=-1)

        nparams_0 = self.latent_prior.value.natural_parameters
        for i, l_post in enumerate(l_posts):
            l_post.natural_parameters = nparams_0 + acc_stats[i]

        if not cache:
            return l_posts

        # TODO: possible optimization by doing a single for-loop.
        H = torch.cat([pdf.moments()[0][None, :]
                       for pdf in l_posts], dim=0)
        HH = torch.cat([pdf.moments()[1][None, :]
                        for pdf in l_posts], dim=0)
        cache = {
            'U': U,
            'opt': opt,
            'hessians': hessians,
            'tr_hessians': tr_hessians,
            'm': m,
            'mm': mm,
            'W': W,
            'WW': WW,
            'H': H,
            'HH': HH,
            'stats': stats,
            'counts': counts,
            'WHW': WHW,
        }
        return l_posts, cache

    ####################################################################
    # BayesianModel interface.
    ####################################################################

    def accumulate(self, stats):
        counts = stats[:, -1]
        hessians = self.cache['hessians']
        tr_hessians = self.cache['tr_hessians']
        hW = self.cache['hW']
        opt = self.cache['opt']
        m = self.cache['m']
        H = self.cache['H']
        HH = self.cache['HH']

        # Mean stats.
        HWh_o = hessians[:, None] * (opt - hW)
        I = torch.eye(len(m), dtype=m.dtype, device=m.device)
        mean_stats = torch.cat([
            -HWh_o.sum(dim=0),
            .5 * (hessians[:, None, None] * I[None]).sum(dim=0).reshape(-1)
        ], dim=-1)

        # Subspace stats.
        isometric_params = tr_hessians[:, None] / len(m)
        to_m = (opt - m) * isometric_params
        tHH = HH * isometric_params
        subspace_stats = torch.cat([
            -(H.t() @ to_m).reshape(-1),
            .5 * tHH.sum(dim=0)
        ])

        return {
            self.mean: mean_stats,
            self.subspace: subspace_stats
        }


__all__ = [
    'GeneralizedSubspaceModel',
]
