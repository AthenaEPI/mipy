# -*- coding: utf-8 -*-
'''
Document Module
'''
from __future__ import division
import pkg_resources
from collections import OrderedDict

import numpy as np
from time import time

from ..utils.spherical_mean import (
    estimate_spherical_mean_multi_shell)
from ..utils.utils import T1_tortuosity, parameter_equality
from .fitted_modeling_framework import (
    FittedMultiCompartmentModel,
    FittedMultiCompartmentSphericalMeanModel)
from ..optimizers.brute2fine import (
    GlobalBruteOptimizer, Brute2FineOptimizer)
from ..optimizers.mix import MixOptimizer
from dipy.utils.optpkg import optional_package
pathos, have_pathos, _ = optional_package("pathos")

if have_pathos:
    import pathos.pools as pp
    from pathos.helpers import cpu_count

GRADIENT_TABLES_PATH = pkg_resources.resource_filename(
    'dmipy', 'data/gradient_tables'
)
SIGNAL_MODELS_PATH = pkg_resources.resource_filename(
    'dmipy', 'signal_models'
)

__all__ = [
    'ModelProperties',
    'MultiCompartmentModelProperties',
    'MultiCompartmentModel',
    'MultiCompartmentSphericalMeanModel',
    'homogenize_x0_to_data',
    'ReturnFixedValue'
]


class ModelProperties:
    "Contains various properties for CompartmentModels."
    @property
    def parameter_ranges(self):
        """Returns the optimization ranges of the model parameters.
        These ranges are given in O(1) scale so optimization algorithms
        don't suffer from large scale differences in optimization parameters.
        """
        return OrderedDict(self._parameter_ranges.copy())

    @property
    def parameter_scales(self):
        """Returns the optimization scales for the model parameters.
        The scales scale the parameter_ranges to their actual size inside
        optimization algorithms.
        """
        return OrderedDict(self._parameter_scales.copy())

    @property
    def parameter_names(self):
        "Returns the names of model parameters."
        return self._parameter_ranges.keys()

    @property
    def parameter_cardinality(self):
        "Returns the cardinality of model parameters"
        return OrderedDict([
            (k, len(np.atleast_2d(self.parameter_ranges[k])))
            for k in self.parameter_ranges
        ])


class MultiCompartmentModelProperties:
    "Class that contains various properties of MultiCompartmentModel instance."

    @property
    def parameter_names(self):
        "Returns the names of model parameters."
        return list(self.parameter_ranges.keys())

    def parameter_vector_to_parameters(self, parameter_vector):
        """Returns the model parameters in dictionary format according to their
        parameter_names. Takes parameter_vector as input, which is the same as
        the output of a FittedMultiCompartmentModel.fitted_parameter_vector.

        Parameters
        ----------
        parameter_vector: array of size (Ndata_x, Ndata_y, ..., Nparameters),
            array that contains the linearized model parameters for an ND-array
            of data voxels.

        Returns
        -------
        parameter: dictionary with parameter_names as parameter keys,
            contains the model parameters in dictionary format.
        """
        parameters = {}
        current_pos = 0
        if parameter_vector.ndim == 1:
            for parameter, card in self.parameter_cardinality.items():
                parameters[parameter] = parameter_vector[
                    current_pos: current_pos + card
                ]
                current_pos += card
        else:
            for parameter, card in self.parameter_cardinality.items():
                parameters[parameter] = parameter_vector[
                    ..., current_pos: current_pos + card
                ]
                current_pos += card
        return parameters

    def parameters_to_parameter_vector(self, **parameters):
        """Returns the model parameters in array format. The input is a
        parameters dictionary that has parameter_names as keys. This is also
        the output of a FittedMultiCompartmentModel.fitted_parameters.

        It's possible to give an array of values for one parameter and only a
        float for others. The function will automatically assume that that the
        float parameters are constant in the data set and broadcast them
        accordingly.

        The output parameter_vector can be used in simulate_data() to generate
        data according to the given input parameters.

        Parameters
        ----------
        parameters: keyword arguments of parameter_names.
            Can be given as **parameter_dictionary that contains the model
            parameter values.

        Returns
        -------
        parameter_vector: array of size (Ndata_x, Ndata_y, ..., Nparameters),
            array that contains the linearized model parameters for an ND-array
            of data voxels.
        """
        parameter_vector = []
        parameter_shapes = []
        for parameter, card in self.parameter_cardinality.items():
            value = np.atleast_1d(parameters[parameter])
            if card == 1 and not np.all(value.shape == np.r_[1]):
                parameter_shapes.append(value.shape)
            if card == 2 and not np.all(value.shape == np.r_[2]):
                parameter_shapes.append(value.shape[:-1])

        if len(set(parameter_shapes)) > 1:
            msg = "parameter shapes are inconsistent."
            raise ValueError(msg)
        elif len(set(parameter_shapes)) == 0:
            for parameter, card in self.parameter_cardinality.items():
                parameter_vector.append(parameters[parameter])
            parameter_vector = np.hstack(parameter_vector)
        elif len(set(parameter_shapes)) == 1:
            for parameter, card in self.parameter_cardinality.items():
                value = np.atleast_1d(parameters[parameter])
                if card == 1 and np.all(value.shape == np.r_[1]):
                    parameter_vector.append(
                        np.tile(value[0], np.r_[parameter_shapes[0], 1]))
                elif card == 1 and not np.all(value.shape == np.r_[1]):
                    parameter_vector.append(value[..., None])
                elif card == 2 and np.all(value.shape == np.r_[2]):
                    parameter_vector.append(
                        np.tile(value, np.r_[parameter_shapes[0], 1])
                    )
                else:
                    parameter_vector.append(parameters[parameter])
            parameter_vector = np.concatenate(parameter_vector, axis=-1)
        return parameter_vector

    def parameter_initial_guess_to_parameter_vector(self, **parameters):
        """Function that returns a parameter_vector while allowing for partial
        input of model parameters, setting the ones that were not given to
        'None'. Such an array can be given to the fit() function to provide an
        initial parameter guess when fitting the data to the model.

        Parameters
        ----------
        parameters: keyword arguments of parameter names,
            parameter values of only the parameters you want to give as an
            initial condition for the optimizer.

        Returns
        -------
        parameter_vector: array of size (Ndata_x, Ndata_y, ..., Nparameters),
            array that contains the linearized model parameters for an ND-array
            of data voxels, with None's for non-given parameters.
        """
        set_parameters = {}
        parameter_cardinality = self.parameter_cardinality.copy()
        for parameter, value in parameters.items():
            if parameter in self.parameter_cardinality.keys():
                set_parameters[parameter] = value
                del parameter_cardinality[parameter]
            else:
                msg = '"{}" is not a valid model parameter.'.format(parameter)
                raise ValueError(msg)
        if len(parameter_cardinality) == 0:
            print("All model parameters set.")
        else:
            for parameter, card in parameter_cardinality.items():
                set_parameters[parameter] = np.tile(np.nan, card)
        return self.parameters_to_parameter_vector(**set_parameters)

    def _prepare_parameters(self):
        """Prepares the parameter ranges, scales, cadinality and parameter
        upon instantiating the MultiCompartmentModel"""
        self.model_names = []
        model_counts = {}

        for model in self.models:
            if model.__class__ not in model_counts:
                model_counts[model.__class__] = 1
            else:
                model_counts[model.__class__] += 1

            self.model_names.append(
                '{}_{:d}_'.format(
                    model.__class__.__name__,
                    model_counts[model.__class__]
                )
            )

        self.parameter_ranges = OrderedDict({
            model_name + k: v
            for model, model_name in zip(self.models, self.model_names)
            for k, v in model.parameter_ranges.items()
        })

        self.parameter_scales = OrderedDict({
            model_name + k: v
            for model, model_name in zip(self.models, self.model_names)
            for k, v in model.parameter_scales.items()
        })

        self._parameter_map = {
            model_name + k: (model, k)
            for model, model_name in zip(self.models, self.model_names)
            for k in model.parameter_ranges
        }

        self._inverted_parameter_map = {
            v: k for k, v in self._parameter_map.items()
        }

        self.parameter_cardinality = OrderedDict([
            (k, len(np.atleast_2d(self.parameter_ranges[k])))
            for k in self.parameter_ranges
        ])

    def _prepare_partial_volumes(self):
        "Prepares partial volumes upon instantiating the MultiCompartmentModel"
        if len(self.models) > 1:
            self.partial_volume_names = [
                'partial_volume_{:d}'.format(i)
                for i in range(len(self.models))
            ]

            for i, partial_volume_name in enumerate(
                    self.partial_volume_names):
                self.parameter_ranges[partial_volume_name] = (0.01, .99)
                self.parameter_scales[partial_volume_name] = 1.
                self._parameter_map[partial_volume_name] = (
                    None, partial_volume_name
                )
                self._inverted_parameter_map[(None, partial_volume_name)] = \
                    partial_volume_name
                self.parameter_cardinality[partial_volume_name] = 1

    def _prepare_parameter_links(self):
        """Prepares parameter links if given as input to MultiCompartmentModel.
        It first checks if the parameter that will be linked exists. If so,
        then it removes it from the parameter ranges, scales and cardinality,
        so it will not be optimized (as it will be a function of other
        parameters)."""
        for i, parameter_function in enumerate(self.parameter_links):
            parameter_model, parameter_name, parameter_function, arguments = \
                parameter_function

            if (
                (parameter_model, parameter_name)
                not in self._inverted_parameter_map
            ):
                raise ValueError(
                    "Parameter function {} doesn't exist".format(i)
                )

            parameter_name = self._inverted_parameter_map[
                (parameter_model, parameter_name)
            ]

            del self.parameter_ranges[parameter_name]
            del self.parameter_cardinality[parameter_name]
            del self.parameter_scales[parameter_name]

    def _prepare_model_properties(self):
        """Checks that spherical mean and regular models cannot be optimized
        together, and whether the model can estimate a Fiber Orientation
        Distribution (FOD)."""
        models_spherical_mean = [
            model._spherical_mean for model in self.models]
        if len(np.unique(models_spherical_mean)) > 1:
            msg = "Cannot mix spherical mean and non-spherical mean models. "
            msg = "Current model selection is {}".format(self.models)
            raise ValueError(msg)
        self._spherical_mean = np.all(models_spherical_mean)
        self.fod_available = False
        for model in self.models:
            try:
                model.fod
                self.fod_available = True
            except AttributeError:
                pass

    def _check_for_double_model_class_instances(self):
        "Checks all models have unique class instances."
        if len(self.models) != len(set(self.models)):
            msg = "Each model in the multi-compartment model must be "
            msg += "instantiated separately. For example, to make a model "
            msg += "with two sticks, the models must be given as "
            msg += "models = [stick1, stick2], not as "
            msg += "models = [stick1, stick1]."
            raise ValueError(msg)

    def add_linked_parameters_to_parameters(self, parameters):
        """When making the MultiCompartmentModel function call, adds the linked
        parameter to the optimized parameters by evaluating the parameter link
        function."""
        if len(self.parameter_links) == 0:
            return parameters
        parameters = parameters.copy()
        for parameter in self.parameter_links[::-1]:
            parameter_model, parameter_name, parameter_function, arguments = \
                parameter
            parameter_name = self._inverted_parameter_map[
                (parameter_model, parameter_name)
            ]

            if len(arguments) > 0:
                argument_values = []
                for argument in arguments:
                    argument_name = self._inverted_parameter_map[argument]
                    argument_values.append(parameters.get(
                        argument_name  # ,
                        # self.parameter_defaults[argument_name]
                    ))

                parameters[parameter_name] = parameter_function(
                    *argument_values
                )
            else:
                parameters[parameter_name] = parameter_function()
        return parameters

    def _prepare_parameters_to_optimize(self):
        "Sets up which parmameters to optimize."
        self.optimized_parameters = OrderedDict({
            k: True
            for k, v in self.parameter_cardinality.items()
        })

    @property
    def bounds_for_optimization(self):
        "Returns the linear parameter bounds for the model optimization."
        bounds = []
        for parameter, card in self.parameter_cardinality.items():
            range_ = self.parameter_ranges[parameter]
            if card == 1:
                bounds.append(range_)
            else:
                for i in range(card):
                    bounds.append((range_[0][i], range_[1][i]))
        return bounds

    @property
    def opt_params_for_optimization(self):
        "Returns the linear bools whether to optimize a model parameter."
        params = []
        for parameter, card in self.parameter_cardinality.items():
            optimize_param = self.optimized_parameters[parameter]
            if card == 1:
                params.append(optimize_param)
            else:
                for i in range(card):
                    params.append(optimize_param)
        return params

    @property
    def scales_for_optimization(self):
        "Returns the linear parameter scales for model optimization."
        return np.hstack([scale for parameter, scale in
                          self.parameter_scales.items()])

    def set_fixed_parameter(self, parameter_name, value):
        """
        Allows the user to fix an optimization parameter to a static value.
        The fixed parameter will be removed from the optimized parameters and
        added as a linked parameter.

        Parameters
        ----------
        parameter_name: string
            name of the to-be-fixed parameters, see self.parameter_names.
        value: float or list of corresponding parameter_cardinality.
            the value to fix the parameter at in SI units.
        """
        if parameter_name in self.parameter_ranges.keys():
            model, name = self._parameter_map[parameter_name]
            parameter_link = (model, name, ReturnFixedValue(value), [])
            self.parameter_links.append(parameter_link)
            del self.parameter_ranges[parameter_name]
            del self.parameter_cardinality[parameter_name]
            del self.parameter_scales[parameter_name]
        else:
            print('"{}" does not exist or has already been fixed.').format(
                parameter_name)

    def set_tortuous_parameter(self, lambda_perp_parameter_name,
                               lambda_par_parameter_name,
                               volume_fraction_intra_parameter_name,
                               volume_fraction_extra_parameter_name):
        """
        Allows the user to set a tortuosity constraint on the perpendicular
        diffusivity of the extra-axonal compartment, which depends on the
        intra-axonal volume fraction and parallel diffusivity.

        The perpendicular diffusivity parameter will be removed from the
        optimized parameters and added as a linked parameter.

        Parameters
        ----------
        lambda_perp_parameter_name: string
            name of the perpendicular diffusivity parameter, see
            self.parameter_names.
        lambda_par_parameter_name: string
            name of the parallel diffusivity parameter, see
            self.parameter_names.
        volume_fraction_intra_parameter_name: string
            name of the intra-axonal volume fraction parameter, see
            self.parameter_names.
        volume_fraction_extra_parameter_name: string
            name of the extra-axonal volume fraction parameter, see
            self.parameter_names.
        """
        params = [lambda_perp_parameter_name, lambda_par_parameter_name,
                  volume_fraction_intra_parameter_name,
                  volume_fraction_extra_parameter_name]
        for param in params:
            try:
                self.parameter_cardinality[param]
            except KeyError:
                msg = ("{} does not exist or has already been fixed.").format(
                    param)
                raise ValueError(msg)

        model, name = self._parameter_map[lambda_perp_parameter_name]
        self.parameter_links.append([model, name, T1_tortuosity, [
            self._parameter_map[lambda_par_parameter_name],
            self._parameter_map[volume_fraction_intra_parameter_name],
            self._parameter_map[volume_fraction_extra_parameter_name]]
        ])
        del self.parameter_ranges[lambda_perp_parameter_name]
        del self.parameter_cardinality[lambda_perp_parameter_name]
        del self.parameter_scales[lambda_perp_parameter_name]

    def set_equal_parameter(self, parameter_name_in, parameter_name_out):
        """
        Allows the user to set two parameters equal to each other. This is used
        for example in the NODDI model to set the parallel diffusivities of the
        Stick and Zeppelin compartment to the same value.

        The second input parameter will be removed from the optimized
        parameters and added as a linked parameter.

        Parameters
        ----------
        parameter_name_in: string
            the first parameter name, see self.parameter_names.
        parameter_name_out: string,
            the second parameter name, see self.parameter_names. This is the
            parameter that will be removed form the optimzed parameters.
        """
        params = [parameter_name_in, parameter_name_out]
        for param in params:
            try:
                self.parameter_cardinality[param]
            except KeyError:
                msg = ("{} does not exist or has already been fixed.").format(
                    param)
                raise ValueError(msg)
        model, name = self._parameter_map[parameter_name_out]
        self.parameter_links.append([model, name, parameter_equality, [
            self._parameter_map[parameter_name_in]]])
        del self.parameter_ranges[parameter_name_out]
        del self.parameter_cardinality[parameter_name_out]
        del self.parameter_scales[parameter_name_out]


class MultiCompartmentModel(MultiCompartmentModelProperties):
    r'''
    The MultiCompartmentModel class allows to combine any number of
    CompartmentModels and DistributedModels into one combined model that can
    be used to fit and simulate dMRI data.

    Parameters
    ----------
    models : list of N CompartmentModel instances,
        the models to combine into the MultiCompartmentModel.
    parameter_links : list of iterables (model, parameter name, link function,
        argument list),
        deprecated, for testing only.
    '''

    def __init__(self, models, parameter_links=None):
        self.models = models
        self.parameter_links = parameter_links
        if parameter_links is None:
            self.parameter_links = []

        self._prepare_parameters()
        self._prepare_partial_volumes()
        self._prepare_parameter_links()
        self._prepare_model_properties()
        self._check_for_double_model_class_instances()
        self._prepare_parameters_to_optimize()

    def fit(self, acquisition_scheme, data, parameter_initial_guess=None,
            mask=None, solver='brute2fine', Ns=5, maxiter=300,
            N_sphere_samples=30, use_parallel_processing=have_pathos,
            number_of_processors=None):
        """ The main data fitting function of a MultiCompartmentModel.

        This function can fit it to an N-dimensional dMRI data set, and returns
        a FittedMultiCompartmentModel instance that contains the fitted
        parameters and other useful functions to study the results.

        No initial guess needs to be given to fit a model, but a partial or
        complete initial guess can be given if the user wants to have a
        solution that is a local minimum close to that guess. The
        parameter_initial_guess input can be created using
        parameter_initial_guess_to_parameter_vector().

        A mask can also be given to exclude voxels from fitting (e.g. voxels
        that are outside the brain). If no mask is given then all voxels are
        included.

        An optimization approach can be chosen as either 'brute2fine' or 'mix'.
        - Choosing brute2fine will first use a brute-force optimization to find
          an initial guess for parameters without one, and will then refine the
          result using gradient-descent-based optimization.

          Note that given no initial guess will make brute2fine precompute an
          global parameter grid that will be re-used for all voxels, which in
          many cases is much faster than giving voxel-varying initial condition
          that requires a grid to be estimated per voxel.

        - Choosing mix will use the recent MIX algorithm based on separation of
          linear and non-linear parameters. MIX first uses a stochastic
          algorithm to find the non-linear parameters (non-volume fractions),
          then estimates the volume fractions while fixing the estimates of the
          non-linear parameters, and then finally refines the solution using
          a gradient-descent-based algorithm.

        The fitting process can be readily parallelized using the optional
        "pathos" package. If it is installed then it will automatically use it,
        but it can be turned off by setting use_parallel_processing=False. The
        algorithm will automatically use all cores in the machine, unless
        otherwise specified in number_of_processors.

        Data with multiple TE are normalized in separate segments using the
        b0-values according that TE.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        data : N-dimensional array of size (N_x, N_y, ..., N_dwis),
            The measured DWI signal attenuation array of either a single voxel
            or an N-dimensional dataset.
        parameter_initial_guess: parameter array,
            must be of size (Nparameters,) or the same size as the data.
        mask : (N-1)-dimensional integer/boolean array of size (N_x, N_y, ...),
            Optional mask of voxels to be included in the optimization.
        solver : string,
            Selection of optimization algorithm.
            - 'brute2fine' to use brute-force optimization.
            - 'mix' to use Microstructure Imaging of Crossing (MIX)
              optimization.
        Ns : integer,
            for brute optimization, decised how many steps are sampled for
            every parameter.
        maxiter : integer,
            for MIX optimization, how many iterations are allowed.
        N_sphere_samples : integer,
            for brute optimization, how many spherical orientations are sampled
            for 'mu'.
        use_parallel_processing : bool,
            whether or not to use parallel processing using pathos.
        number_of_processors : integer,
            number of processors to use for parallel processing. Defaults to
            the number of processors in the computer according to cpu_count().

        Returns
        -------
        FittedCompartmentModel: class instance that contains fitted parameters,
            Can be used to recover parameters themselves or other useful
            functions.
        """

        # estimate S0
        self.scheme = acquisition_scheme
        data_ = np.atleast_2d(data)
        if self.scheme.TE is None:  # if no TE is given
            S0 = np.mean(data_[..., self.scheme.b0_mask], axis=-1)
        else:  # if multiple TE are in the data
            S0 = np.ones_like(data_)
            for TE_ in self.scheme.shell_TE:
                TE_mask = self.scheme.TE == TE_
                TE_b0_mask = np.all([self.scheme.b0_mask, TE_mask], axis=0)
                S0[..., TE_mask] = np.mean(
                    data_[..., TE_b0_mask], axis=-1)[..., None]

        if mask is None:
            mask = data_[..., 0] > 0
        else:
            mask = np.all([mask, data_[..., 0] > 0], axis=0)
        mask_pos = np.where(mask)

        N_parameters = len(self.bounds_for_optimization)
        N_voxels = np.sum(mask)

        # make starting parameters and data the same size
        if parameter_initial_guess is None:
            x0_ = np.tile(np.nan,
                          np.r_[data_.shape[:-1], N_parameters])
        else:
            x0_ = homogenize_x0_to_data(
                data_, parameter_initial_guess)
            x0_bool = np.all(
                np.isnan(x0_), axis=tuple(np.arange(x0_.ndim - 1)))
            x0_[..., ~x0_bool] /= self.scales_for_optimization[~x0_bool]

        if use_parallel_processing and not have_pathos:
            msg = 'Cannot use parallel processing without pathos.'
            raise ValueError(msg)
        elif use_parallel_processing and have_pathos:
            fitted_parameters_lin = [None] * N_voxels
            if number_of_processors is None:
                number_of_processors = cpu_count()
            pool = pp.ProcessPool(number_of_processors)
            print('Using parallel processing with {} workers.'.format(
                number_of_processors))
        else:
            fitted_parameters_lin = np.empty(
                np.r_[N_voxels, N_parameters], dtype=float)

        # if the models are spherical mean based then estimate the
        # spherical mean of the data.
        if self._spherical_mean:
            data_to_fit = np.zeros(
                np.r_[data_.shape[:-1],
                      self.scheme.unique_dwi_indices.max() + 1])
            for pos in zip(*mask_pos):
                data_to_fit[pos] = estimate_spherical_mean_multi_shell(
                    data_[pos], self.scheme)
        else:
            data_to_fit = data_

        start = time()
        if solver == 'brute2fine':
            global_brute = GlobalBruteOptimizer(
                self, self.scheme,
                parameter_initial_guess, Ns, N_sphere_samples)
            fit_func = Brute2FineOptimizer(self, self.scheme, Ns)
            print('Setup brute2fine optimizer in {} seconds'.format(
                time() - start))
        elif solver == 'mix':
            fit_func = MixOptimizer(self, self.scheme, maxiter)
            print('Setup MIX optimizer in {} seconds'.format(
                time() - start))

        start = time()
        for idx, pos in enumerate(zip(*mask_pos)):
            voxel_E = data_to_fit[pos] / S0[pos]
            voxel_x0_vector = x0_[pos]
            if solver == 'brute2fine':
                if global_brute.global_optimization_grid is True:
                    voxel_x0_vector = global_brute(voxel_E)
            fit_args = (voxel_E, voxel_x0_vector)

            if use_parallel_processing:
                fitted_parameters_lin[idx] = pool.apipe(fit_func, *fit_args)
            else:
                fitted_parameters_lin[idx] = fit_func(*fit_args)
        if use_parallel_processing:
            fitted_parameters_lin = np.array(
                [p.get() for p in fitted_parameters_lin])

        fitting_time = time() - start
        print('Fitting of {} voxels complete in {} seconds.'.format(
            len(fitted_parameters_lin), fitting_time))
        print('Average of {} seconds per voxel.'.format(
            fitting_time / N_voxels))

        fitted_parameters = np.zeros_like(x0_, dtype=float)
        fitted_parameters[mask_pos] = (
            fitted_parameters_lin * self.scales_for_optimization)

        return FittedMultiCompartmentModel(
            self, S0, mask, fitted_parameters)

    def simulate_signal(self, acquisition_scheme, model_parameters_array):
        """
        Function to simulate diffusion data for a given acquisition_scheme
        and model parameters for the MultiCompartmentModel.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy
        model_parameters_array : 1D array of size (N_parameters) or
            N-dimensional array the same size as the data.
            The model parameters of the MultiCompartmentModel model.

        Returns
        -------
        E_simulated: 1D array of size (N_parameters) or N-dimensional
            array the same size as x0.
            The simulated signal of the microstructure model.
        """
        Ndata = acquisition_scheme.number_of_measurements
        if self._spherical_mean:
            Ndata = len(acquisition_scheme.shell_bvalues)
        x0 = model_parameters_array

        x0_at_least_2d = np.atleast_2d(x0)
        x0_2d = x0_at_least_2d.reshape(-1, x0_at_least_2d.shape[-1])
        E_2d = np.empty(np.r_[x0_2d.shape[:-1], Ndata])
        for i, x0_ in enumerate(x0_2d):
            parameters = self.parameter_vector_to_parameters(x0_)
            E_2d[i] = self(acquisition_scheme, **parameters)
        E_simulated = E_2d.reshape(
            np.r_[x0_at_least_2d.shape[:-1], Ndata])

        if x0.ndim == 1:
            return np.squeeze(E_simulated)
        else:
            return E_simulated

    def __call__(self, acquisition_scheme_or_vertices,
                 quantity="signal", **kwargs):
        """
        The MultiCompartmentModel function call for to generate signal
        attenuation for a given acquisition scheme and model parameters.

        First, the linked parameters are added to the optimized parameters.

        Then, every model in the MultiCompartmentModel is called with the right
        parameters to recover the part of the signal attenuation of that model.
        The resulting values are multiplied with the volume fractions and
        finally the combined signal attenuation is returned.

        Aside from the signal, the function call can also return the Fiber
        Orientation Distributions (FODs) when a dispersed model is used, and
        can also return the stochastic cost function for the MIX algorithm.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        quantity : string
            can be 'signal', 'FOD' or 'stochastic cost function' depending on
            the need of the model.
        kwargs: keyword arguments to the model parameter values,
            Is internally given as **parameter_dictionary.
        """
        if quantity == "signal" or quantity == "FOD":
            values = 0
        elif quantity == "stochastic cost function":
            values = np.empty((
                acquisition_scheme_or_vertices.number_of_measurements,
                len(self.models)
            ))
            counter = 0

        kwargs = self.add_linked_parameters_to_parameters(
            kwargs
        )
        if len(self.models) > 1:
            partial_volumes = [
                kwargs[p] for p in self.partial_volume_names
            ]
        else:
            partial_volumes = [1.]

        for model_name, model, partial_volume in zip(
            self.model_names, self.models, partial_volumes
        ):
            parameters = {}
            for parameter in model.parameter_ranges:
                parameter_name = self._inverted_parameter_map[
                    (model, parameter)
                ]
                parameters[parameter] = kwargs.get(
                    # , self.parameter_defaults.get(parameter_name)
                    parameter_name
                )

            if quantity == "signal":
                values = (
                    values +
                    partial_volume * model(
                        acquisition_scheme_or_vertices, **parameters)
                )
            elif quantity == "FOD":
                try:
                    values = (
                        values +
                        partial_volume * model.fod(
                            acquisition_scheme_or_vertices, **parameters)
                    )
                except AttributeError:
                    continue
            elif quantity == "stochastic cost function":
                values[:, counter] = model(acquisition_scheme_or_vertices,
                                           **parameters)
                counter += 1
        return values


class MultiCompartmentSphericalMeanModel(MultiCompartmentModelProperties):
    r'''
    The MultiCompartmentModel class allows to combine any number of
    CompartmentModels and DistributedModels into one combined model that can
    be used to fit and simulate dMRI data.

    Parameters
    ----------
    models : list of N CompartmentModel instances,
        the models to combine into the MultiCompartmentModel.
    parameter_links : list of iterables (model, parameter name, link function,
        argument list),
        deprecated, for testing only.
    '''

    def __init__(self, models, parameter_links=None):
        self.models = models
        self.parameter_links = parameter_links
        if parameter_links is None:
            self.parameter_links = []

        self._prepare_parameters()
        self._delete_orientation_parameters()
        self._prepare_partial_volumes()
        self._prepare_parameter_links()
        self._prepare_model_properties()
        self._check_for_double_model_class_instances()
        self._prepare_parameters_to_optimize()

    def _delete_orientation_parameters(self):
        """
        Deletes orientation parameter 'mu' since it's not needed in spherical
        mean models.
        """
        for model in self.models:
            if 'mu' in model.parameter_names:
                parameter_name = self._inverted_parameter_map[(model, 'mu')]
                del self.parameter_ranges[parameter_name]
                del self.parameter_cardinality[parameter_name]
                del self.parameter_scales[parameter_name]

    def fit(self, acquisition_scheme, data, parameter_initial_guess=None,
            mask=None, solver='brute2fine', Ns=5, maxiter=300,
            N_sphere_samples=30, use_parallel_processing=have_pathos,
            number_of_processors=None):
        """ The main data fitting function of a MultiCompartmentModel.

        This function can fit it to an N-dimensional dMRI data set, and returns
        a FittedMultiCompartmentModel instance that contains the fitted
        parameters and other useful functions to study the results.

        No initial guess needs to be given to fit a model, but a partial or
        complete initial guess can be given if the user wants to have a
        solution that is a local minimum close to that guess. The
        parameter_initial_guess input can be created using
        parameter_initial_guess_to_parameter_vector().

        A mask can also be given to exclude voxels from fitting (e.g. voxels
        that are outside the brain). If no mask is given then all voxels are
        included.

        An optimization approach can be chosen as either 'brute2fine' or 'mix'.
        - Choosing brute2fine will first use a brute-force optimization to find
          an initial guess for parameters without one, and will then refine the
          result using gradient-descent-based optimization.

          Note that given no initial guess will make brute2fine precompute an
          global parameter grid that will be re-used for all voxels, which in
          many cases is much faster than giving voxel-varying initial condition
          that requires a grid to be estimated per voxel.

        - Choosing mix will use the recent MIX algorithm based on separation of
          linear and non-linear parameters. MIX first uses a stochastic
          algorithm to find the non-linear parameters (non-volume fractions),
          then estimates the volume fractions while fixing the estimates of the
          non-linear parameters, and then finally refines the solution using
          a gradient-descent-based algorithm.

        The fitting process can be readily parallelized using the optional
        "pathos" package. If it is installed then it will automatically use it,
        but it can be turned off by setting use_parallel_processing=False. The
        algorithm will automatically use all cores in the machine, unless
        otherwise specified in number_of_processors.

        Data with multiple TE are normalized in separate segments using the
        b0-values according that TE.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        data : N-dimensional array of size (N_x, N_y, ..., N_dwis),
            The measured DWI signal attenuation array of either a single voxel
            or an N-dimensional dataset.
        parameter_initial_guess: parameter array,
            must be of size (Nparameters,) or the same size as the data.
        mask : (N-1)-dimensional integer/boolean array of size (N_x, N_y, ...),
            Optional mask of voxels to be included in the optimization.
        solver : string,
            Selection of optimization algorithm.
            - 'brute2fine' to use brute-force optimization.
            - 'mix' to use Microstructure Imaging of Crossing (MIX)
              optimization.
        Ns : integer,
            for brute optimization, decised how many steps are sampled for
            every parameter.
        maxiter : integer,
            for MIX optimization, how many iterations are allowed.
        N_sphere_samples : integer,
            for brute optimization, how many spherical orientations are sampled
            for 'mu'.
        use_parallel_processing : bool,
            whether or not to use parallel processing using pathos.
        number_of_processors : integer,
            number of processors to use for parallel processing. Defaults to
            the number of processors in the computer according to cpu_count().

        Returns
        -------
        FittedCompartmentModel: class instance that contains fitted parameters,
            Can be used to recover parameters themselves or other useful
            functions.
        """

        # estimate S0
        self.scheme = acquisition_scheme
        data_ = np.atleast_2d(data)
        if self.scheme.TE is None:  # if no TE is given
            S0 = np.mean(data_[..., self.scheme.b0_mask], axis=-1)
        else:  # if multiple TE are in the data
            S0 = np.ones_like(len(acquisition_scheme.shell_TE))
            for TE_ in self.scheme.shell_TE:
                TE_mask = self.scheme.TE == TE_
                TE_b0_mask = np.all([self.scheme.shell_b0_mask, TE_mask],
                                    axis=0)
                S0[..., TE_mask] = np.mean(
                    data_[..., TE_b0_mask], axis=-1)[..., None]

        if mask is None:
            mask = data_[..., 0] > 0
        else:
            mask = np.all([mask, data_[..., 0] > 0], axis=0)
        mask_pos = np.where(mask)

        N_parameters = len(self.bounds_for_optimization)
        N_voxels = np.sum(mask)

        # make starting parameters and data the same size
        if parameter_initial_guess is None:
            x0_ = np.tile(np.nan,
                          np.r_[data_.shape[:-1], N_parameters])
        else:
            x0_ = homogenize_x0_to_data(
                data_, parameter_initial_guess)
            x0_bool = np.all(
                np.isnan(x0_), axis=tuple(np.arange(x0_.ndim - 1)))
            x0_[..., ~x0_bool] /= self.scales_for_optimization[~x0_bool]

        if use_parallel_processing and not have_pathos:
            msg = 'Cannot use parallel processing without pathos.'
            raise ValueError(msg)
        elif use_parallel_processing and have_pathos:
            fitted_parameters_lin = [None] * N_voxels
            if number_of_processors is None:
                number_of_processors = cpu_count()
            pool = pp.ProcessPool(number_of_processors)
            print('Using parallel processing with {} workers.'.format(
                number_of_processors))
        else:
            fitted_parameters_lin = np.empty(
                np.r_[N_voxels, N_parameters], dtype=float)

        # estimate the spherical mean of the data.
        data_to_fit = np.zeros(
            np.r_[data_.shape[:-1],
                  self.scheme.unique_dwi_indices.max() + 1])
        for pos in zip(*mask_pos):
            data_to_fit[pos] = estimate_spherical_mean_multi_shell(
                data_[pos], self.scheme)

        start = time()
        if solver == 'brute2fine':
            global_brute = GlobalBruteOptimizer(
                self, self.scheme,
                parameter_initial_guess, Ns, N_sphere_samples)
            fit_func = Brute2FineOptimizer(self, self.scheme, Ns)
            print('Setup brute2fine optimizer in {} seconds'.format(
                time() - start))
        elif solver == 'mix':
            fit_func = MixOptimizer(self, self.scheme, maxiter)
            print('Setup MIX optimizer in {} seconds'.format(
                time() - start))

        start = time()
        for idx, pos in enumerate(zip(*mask_pos)):
            voxel_E = data_to_fit[pos] / S0[pos]
            voxel_x0_vector = x0_[pos]
            if solver == 'brute2fine':
                if global_brute.global_optimization_grid is True:
                    voxel_x0_vector = global_brute(voxel_E)
            fit_args = (voxel_E, voxel_x0_vector)

            if use_parallel_processing:
                fitted_parameters_lin[idx] = pool.apipe(fit_func, *fit_args)
            else:
                fitted_parameters_lin[idx] = fit_func(*fit_args)
        if use_parallel_processing:
            fitted_parameters_lin = np.array(
                [p.get() for p in fitted_parameters_lin])

        fitting_time = time() - start
        print('Fitting of {} voxels complete in {} seconds.'.format(
            len(fitted_parameters_lin), fitting_time))
        print('Average of {} seconds per voxel.'.format(
            fitting_time / N_voxels))

        fitted_parameters = np.zeros_like(x0_, dtype=float)
        fitted_parameters[mask_pos] = (
            fitted_parameters_lin * self.scales_for_optimization)

        return FittedMultiCompartmentSphericalMeanModel(
            self, S0, mask, fitted_parameters)

    def simulate_signal(self, acquisition_scheme, model_parameters_array):
        """
        Function to simulate diffusion data for a given acquisition_scheme
        and model parameters for the MultiCompartmentModel.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy
        model_parameters_array : 1D array of size (N_parameters) or
            N-dimensional array the same size as the data.
            The model parameters of the MultiCompartmentModel model.

        Returns
        -------
        E_simulated: 1D array of size (N_parameters) or N-dimensional
            array the same size as x0.
            The simulated signal of the microstructure model.
        """
        Ndata = len(acquisition_scheme.shell_bvalues)
        x0 = model_parameters_array

        x0_at_least_2d = np.atleast_2d(x0)
        x0_2d = x0_at_least_2d.reshape(-1, x0_at_least_2d.shape[-1])
        E_2d = np.empty(np.r_[x0_2d.shape[:-1], Ndata])
        for i, x0_ in enumerate(x0_2d):
            parameters = self.parameter_vector_to_parameters(x0_)
            E_2d[i] = self(acquisition_scheme, **parameters)
        E_simulated = E_2d.reshape(
            np.r_[x0_at_least_2d.shape[:-1], Ndata])

        if x0.ndim == 1:
            return np.squeeze(E_simulated)
        else:
            return E_simulated

    def __call__(self, acquisition_scheme_or_vertices,
                 quantity="signal", **kwargs):
        """
        The MultiCompartmentModel function call for to generate signal
        attenuation for a given acquisition scheme and model parameters.

        First, the linked parameters are added to the optimized parameters.

        Then, every model in the MultiCompartmentModel is called with the right
        parameters to recover the part of the signal attenuation of that model.
        The resulting values are multiplied with the volume fractions and
        finally the combined signal attenuation is returned.

        Aside from the signal, the function call can also return the Fiber
        Orientation Distributions (FODs) when a dispersed model is used, and
        can also return the stochastic cost function for the MIX algorithm.

        Parameters
        ----------
        acquisition_scheme : DmipyAcquisitionScheme instance,
            An acquisition scheme that has been instantiated using dMipy.
        quantity : string
            can be 'signal', 'FOD' or 'stochastic cost function' depending on
            the need of the model.
        kwargs: keyword arguments to the model parameter values,
            Is internally given as **parameter_dictionary.
        """
        if quantity == "signal":
            values = 0
        elif quantity == "stochastic cost function":
            values = np.empty((
                len(acquisition_scheme_or_vertices.shell_bvalues),
                len(self.models)
            ))
            counter = 0

        kwargs = self.add_linked_parameters_to_parameters(
            kwargs
        )
        if len(self.models) > 1:
            partial_volumes = [
                kwargs[p] for p in self.partial_volume_names
            ]
        else:
            partial_volumes = [1.]

        for model_name, model, partial_volume in zip(
            self.model_names, self.models, partial_volumes
        ):
            parameters = {}
            for parameter in model.parameter_ranges:
                parameter_name = self._inverted_parameter_map[
                    (model, parameter)
                ]
                parameters[parameter] = kwargs.get(
                    # , self.parameter_defaults.get(parameter_name)
                    parameter_name
                )

            if quantity == "signal":
                values = (
                    values +
                    partial_volume * model.spherical_mean(
                        acquisition_scheme_or_vertices, **parameters)
                )
            elif quantity == "stochastic cost function":
                values[:, counter] = model.spherical_mean(
                    acquisition_scheme_or_vertices,
                    **parameters)
                counter += 1
        return values


def homogenize_x0_to_data(data, x0):
    """
    Function that checks if data and initial guess x0 are of the same size.
    If x0 is 1D, it will be tiled to be the same size as data.
    """
    if x0 is not None:
        if x0.ndim == 1:
            # the same x0 will be used for every voxel in N-dimensional data.
            x0_as_data = np.tile(x0, np.r_[data.shape[:-1], 1])
        else:
            x0_as_data = x0.copy()
    if not np.all(
        x0_as_data.shape[:-1] == data.shape[:-1]
    ):
        # if x0 and data are both N-dimensional but have different shapes.
        msg = "data and x0 both N-dimensional but have different shapes. "
        msg += "Current shapes are {} and {}.".format(
            data.shape[:-1],
            x0_as_data.shape[:-1])
        raise ValueError(msg)
    return x0_as_data


class ReturnFixedValue:
    "Parameter fixing class for parameter links."

    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value
