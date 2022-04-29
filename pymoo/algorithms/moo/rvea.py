import numpy as np

from pymoo.algorithms.base.genetic import GeneticAlgorithm
from pymoo.core.survival import Survival
from pymoo.docs import parse_doc_string
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.sampling.rnd import FloatRandomSampling
from pymoo.operators.selection.rnd import RandomSelection
from pymoo.util.display import MultiObjectiveDisplay
from pymoo.util.misc import has_feasible, vectorized_cdist
from pymoo.util.reference_direction import default_ref_dirs
from pymoo.termination.max_eval import MaximumFunctionCallTermination
from pymoo.termination.max_gen import MaximumGenerationTermination


# ---------------------------------------------------------------------------------------------------------
# Algorithm
# ---------------------------------------------------------------------------------------------------------

class RVEA(GeneticAlgorithm):

    def __init__(self,
                 ref_dirs=None,
                 alpha=2.0,
                 adapt_freq=0.1,
                 pop_size=None,
                 sampling=FloatRandomSampling(),
                 selection=RandomSelection(),
                 crossover=SBX(),
                 mutation=PM(),
                 survival=None,
                 eliminate_duplicates=True,
                 n_offsprings=None,
                 display=MultiObjectiveDisplay(),
                 **kwargs):
        """

        Parameters
        ----------

        ref_dirs : {ref_dirs}
        adapt_freq : float
            Defines the ratio of generation when the reference directions are updated.
        pop_size : int (default = None)
            By default the population size is set to None which means that it will be equal to the number of reference
            line. However, if desired this can be overwritten by providing a positive number.
        sampling : {sampling}
        selection : {selection}
        crossover : {crossover}
        mutation : {mutation}
        eliminate_duplicates : {eliminate_duplicates}
        n_offsprings : {n_offsprings}

        """

        # set reference directions and pop_size
        self.ref_dirs = ref_dirs
        if self.ref_dirs is not None:
            if pop_size is None:
                pop_size = len(self.ref_dirs)

        # the alpha value of RVEA
        self.alpha = alpha

        # the fraction of n_max_gen when the the reference directions are adapted
        self.adapt_freq = adapt_freq

        super().__init__(pop_size=pop_size,
                         sampling=sampling,
                         selection=selection,
                         crossover=crossover,
                         mutation=mutation,
                         survival=survival,
                         eliminate_duplicates=eliminate_duplicates,
                         n_offsprings=n_offsprings,
                         display=display,
                         **kwargs)

    def _setup(self, problem, **kwargs):

        # if no reference directions have been provided get them and override the population size and other settings
        if self.ref_dirs is None:
            self.ref_dirs = default_ref_dirs(problem.n_obj)
            self.pop_size, self.n_offsprings = len(self.ref_dirs), len(self.ref_dirs)

        if self.survival is None:
            self.survival = APDSurvival(self.ref_dirs, alpha=self.alpha)

        # the number of adaptions so far (initialized by one)
        self.n_adapt = 1

    def _advance(self, **kwargs):
        super()._advance(**kwargs)

        if self.termination.perc / self.adapt_freq >= self.n_adapt:
            self.survival.adapt()
            self.n_adapt += 1

    def _set_optimum(self, **kwargs):
        if not has_feasible(self.pop):
            self.opt = self.pop[[np.argmin(self.pop.get("CV"))]]
        else:
            self.opt = self.pop


# ---------------------------------------------------------------------------------------------------------
# Survival Selection
# ---------------------------------------------------------------------------------------------------------

def calc_gamma(V):
    gamma = np.arccos((- np.sort(-1 * V @ V.T))[:, 1])
    gamma = np.maximum(gamma, 1e-64)
    return gamma


def calc_V(ref_dirs):
    return ref_dirs / np.linalg.norm(ref_dirs, axis=1)[:, None]


class APDSurvival(Survival):

    def __init__(self, ref_dirs, alpha=2.0) -> None:
        super().__init__(filter_infeasible=True)
        n_dim = ref_dirs.shape[1]

        self.alpha = alpha
        self.niches = None
        self.V, self.gamma = None, None
        self.ideal, self.nadir = np.full(n_dim, np.inf), None

        self.ref_dirs = ref_dirs
        self.extreme_ref_dirs = np.where(np.any(vectorized_cdist(self.ref_dirs, np.eye(n_dim)) == 0, axis=1))[0]

        self.V = calc_V(self.ref_dirs)
        self.gamma = calc_gamma(self.V)

    def adapt(self):
        if self.ideal is not None and self.nadir is not None:
            self.V = calc_V(calc_V(self.ref_dirs) * (self.nadir - self.ideal))
            self.gamma = calc_gamma(self.V)

    def _do(self, problem, pop, n_survive=None, algorithm=None, **kwargs):
        termination = algorithm.termination

        if type(termination) not in [MaximumGenerationTermination, MaximumFunctionCallTermination]:
            pass
            # raise Exception("WARNING: RVEA needs either n_gen or n_evals as a termination criterion.")

        progress = termination.perc

        # get the objective space values
        F = pop.get("F")

        # store the ideal and nadir point estimation for adapt - (and ideal for transformation)
        self.ideal = np.minimum(F.min(axis=0), self.ideal)

        # translate the population to make the ideal point the origin
        F = F - self.ideal

        # the distance to the ideal point
        dist_to_ideal = np.linalg.norm(F, axis=1)
        dist_to_ideal[dist_to_ideal < 1e-64] = 1e-64

        # normalize by distance to ideal
        F_prime = F / dist_to_ideal[:, None]

        # calculate for each solution the acute angles to ref dirs
        acute_angle = np.arccos(F_prime @ self.V.T)
        niches = acute_angle.argmin(axis=1)

        # assign to each reference direction the solution
        niches_to_ind = [[] for _ in range(len(self.V))]
        for k, i in enumerate(niches):
            niches_to_ind[i].append(k)

        # all individuals which will be surviving
        survivors = []

        # for each reference direction
        for k in range(len(self.V)):

            # individuals assigned to the niche
            assigned_to_niche = niches_to_ind[k]

            # if niche not empty
            if len(assigned_to_niche) > 0:
                # the angle of niche to nearest neighboring niche
                gamma = self.gamma[k]

                # the angle from the individuals of this niches to the niche itself
                theta = acute_angle[assigned_to_niche, k]

                # the penalty which is applied for the metric
                M = problem.n_obj if problem.n_obj > 2.0 else 1.0
                penalty = M * (progress ** self.alpha) * (theta / gamma)

                # calculate the angle-penalized penalized (APD)
                apd = dist_to_ideal[assigned_to_niche] * (1 + penalty)

                # the individual which survives
                survivor = assigned_to_niche[apd.argmin()]

                # set attributes to the individual
                pop[assigned_to_niche].set(theta=theta, apd=apd, niche=k, opt=False)
                pop[survivor].set("opt", True)

                # select the one with smallest APD value
                survivors.append(survivor)

        ret = pop[survivors]
        self.niches = niches_to_ind
        self.nadir = ret.get("F").max(axis=0)

        return ret


parse_doc_string(RVEA.__init__)
