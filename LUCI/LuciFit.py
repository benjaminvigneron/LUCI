import numpy as np
from scipy.optimize import minimize
from scipy import interpolate
from tensorflow import keras
from scipy.optimize import Bounds
from numdifftools import Jacobian, Hessian
import emcee
import scipy.special as sps
import scipy.stats as spst
import astropy.stats as astrostats
import warnings
import matplotlib.pyplot as plt
#import corner
warnings.filterwarnings("ignore")

from LUCI.LuciFunctions import Gaussian, Sinc, SincGauss
from LUCI.LuciFitParameters import calculate_vel, calculate_vel_err, calculate_broad, calculate_broad_err, calculate_flux, calculate_flux_err
from LUCI.LuciBayesian import log_probability



class Fit:
    """
    Class that defines the functions necessary for the modelling aspect. This includes
    the gaussian fit functions, the prior definitions, the log likelihood, and the
    definition of the posterior (log likelihood times prior).

    All the functions (gauss, sinc, and sincgauss) are stored in `LuciFuncitons.py`.

    All functions for calculating the velocity, broadening, and flux are in 'LuciFitParameters.py'.

    All the functions for Bayesian Inference with the exception of the fit call
    are in 'LuciBayesian.py'.

    The initial arguments are as follows:
    Args:

        spectrum: Spectrum of interest. This should not be the interpolated spectrum nor normalized(numpy array)

        axis: Wavelength Axis of Spectrum (numpy array)

        wavenumbers_syn: Wavelength Axis of Reference Spectrum (numpy array)

        model_type: Type of model ('gaussian')

        lines: Lines to fit (must be in line_dict)

        sigma_rel: Constraints on sigma (must be list)

        ML_model: Tensorflow/keras machine learning model
    """

    def __init__(self, spectrum, axis, wavenumbers_syn, model_type, lines, vel_rel, sigma_rel,
                 ML_model, trans_filter=None,
                 theta=0, delta_x=2943, n_steps=842, zpd_index=169, filter='SN3',
                 bayes_bool=False, uncertainty_bool=False, mdn=False):
        """
        Args:
            spectrum: Spectrum of interest. This should not be the interpolated spectrum nor normalized(numpy array)
            axis: Wavelength Axis of Spectrum after Redshift Application (numpy array)
            axis_unshifted: Wavelength Axis of Spectrum after Redshift Application (numpy array)
            wavenumbers_syn: Wavelength Axis of Reference Spectrum (numpy array)
            model_type: Type of model ('gaussian')
            lines: Lines to fit (must be in line_dict)
            vel_rel: Constraints on Velocity/Position (must be list; e.x. [1, 2, 1])
            sigma_rel: Constraints on sigma (must be list; e.x. [1, 2, 1])
            ML_model: Tensorflow/keras machine learning model
            trans_filter: Tranmission filter interpolated on unredshifted spectral axis
            theta: Interferometric angle in degrees (defaults to 11.960 -- this is so that the correction coeff is 1)
            delta_x: Step Delta
            n_steps: Number of steps in spectra
            zpd_index: Zero Path Difference index
            filter: SITELLE filter (e.x. 'SN3')
            bayes_bool: Boolean to determine whether or not to run Bayesian analysis (default False)
            uncertainty_bool: Boolean to determine whether or not to run the uncertainty analysis (default False)
            mdn: Boolean to determine which network to use (if true use MDN if false use standard CNN)
        """
        self.line_dict = {'Halpha': 656.280, 'NII6583': 658.341, 'NII6548': 654.803,
                          'SII6716': 671.647, 'SII6731': 673.085, 'OII3726': 372.603,
                          'OII3729': 372.882, 'OIII4959': 495.891, 'OIII5007': 500.684,
                          'Hbeta': 486.133}
        self.available_functions = ['gaussian', 'sinc', 'sincgauss']
        self.spectrum = spectrum
        self.spectrum_clean = spectrum/ np.max(spectrum)  # Clean normalized spectrum that will be used for calculating the noise
        self.axis = axis  # Redshifted axis
        #self.axis_unshifted = axis_unshifted  # Non-redshifted axis
        self.wavenumbers_syn = wavenumbers_syn
        self.model_type = model_type
        self.lines = lines
        self.line_num = len(lines)  # Number of  lines to fit
        self.trans_filter = trans_filter
        if trans_filter is not None:
            self.apply_transmission()  # Apply transmission filter if one is provided
        self.filter = filter
        self.spectrum_interpolated = np.zeros_like(self.spectrum)
        self.spectrum_normalized = self.spectrum / np.max(self.spectrum)  # Normalized spectrum
        self.spectrum_interp_norm = np.zeros_like(self.spectrum)
        self.restrict_wavelength()
        #try:
        self.spectrum_restricted_norm = self.spectrum_restricted/np.max(self.spectrum_restricted)
        #except ValueError:  # self.spectrum_restricted is empty
        #    self.spectrum_restricted_norm = self.spectrum_restricted
        self.theta = theta
        self.cos_theta = np.abs(np.cos(np.deg2rad(self.theta)))
        self.correction_factor = 1.0  # Initialize Correction factor
        self.axis_step = 0.0  # Initialize
        self.delta_x = delta_x
        self.n_steps = n_steps
        self.zpd_index = zpd_index
        self.calculate_correction()
        # Update axis with correction factor
        #self.axis = self.axis*self.correction_factor
        # Calculate noise
        self.noise = 1e-2  # Initialize
        self.calculate_noise()
        self.sigma_rel = sigma_rel
        self.vel_rel = vel_rel
        # ADD ML_MODEL
        self.ML_model = ML_model
        self.bayes_bool = bayes_bool
        self.uncertainty_bool = uncertainty_bool
        self.spectrum_scale = 0.0  # Sacling factor used to normalize spectrum
        self.sinc_width = 0.0  # Width of the sinc function -- Initialize to zero
        self.calc_sinc_width()
        self.mdn = mdn
        self.vel_ml = 0.0  # ML Estimate of the velocity [km/s]
        self.broad_ml = 0.0  # ML Estimate of the velocity dispersion [km/s]
        self.vel_ml_sigma = 0.0  # ML Estimate for velocity 1-sigma error
        self.broad_ml_sigma = 0.0  # ML Estimate for velocity dispersion 1-sigma error
        self.fit_sol = np.zeros(3 * self.line_num + 1)  # Solution to the fit
        self.uncertainties = np.zeros(3 * self.line_num + 1)  # 1-sigma errors on fit parameters
        # Set bounds
        self.A_min = -0.5;
        self.A_max = 1.1;
        self.x_min = 0 #  14700;
        self.x_max = 1e6 #  15600
        self.sigma_min = 0.001;
        self.sigma_max = 300
        # Check that lines inputted by user are in line_dict
        self.check_lines()
        self.check_fitting_model()
        self.check_lengths()


    def apply_transmission(self):
        """
        Apply transmission curve on the spectra according to un-redshifted axis.
        This is done before we interpolate onto the wavenumbers_syn so that the axis
        align properly. Note -- the values of the x-axis are not important for this
        division since we have already interpolated the transition filter vector
        over the UNSHIFTED spectral axis.
        """
        self.spectrum = [self.spectrum[i]/self.trans_filter[i] if self.trans_filter[i] > 0.5 else self.spectrum[i] for i in range(len(self.spectrum))]


    def calculate_correction(self):
        """
        Calculate correction factor based of interferometric angle. This is used to correct the broadening
        """
        self.correction_factor = 1/self.cos_theta
        self.axis_step = self.correction_factor / (2*self.delta_x*(self.n_steps-self.zpd_index)) * 1e7


    def calc_sinc_width(self,):
        """
        Calculate sinc width of the sincgauss function


        """
        MPD = self.cos_theta*self.delta_x*(self.n_steps-self.zpd_index)/1e7
        self.sinc_width = 1/(2*MPD)


    def restrict_wavelength(self):
        """
        Restrict the wavelength range of the fit so that the fit only occurs over the central regions of the spectra.
        We do this so that the continuum is properly calculated.
        """
        # Determine filter
        global bound_lower, bound_upper
        if self.filter == 'SN3':
            bound_lower = 14500
            bound_upper = 15400
        elif self.filter == 'SN2':
            bound_lower = 19500
            bound_upper = 20750
        elif self.filter == 'SN1':
            bound_lower = 26000
            bound_upper = 27400
        elif self.filter == 'C4' and 'Halpha' in self.lines:
            ## This is true for objects at redshift ~0.25
            # In this case we pretend we are in SN3
            bound_lower = 14500
            bound_upper = 15400
        else:
            print('The filter of your datacube is not supported by LUCI. We only support SN1, SN2, and SN3 at the moment.')
        min_ = np.argmin(np.abs(np.array(self.axis)-bound_lower))
        max_ = np.argmin(np.abs(np.array(self.axis)-bound_upper))
        self.spectrum_restricted = self.spectrum_normalized[min_:max_]
        self.axis_restricted = self.axis[min_:max_]
        return min_, max_


    def calculate_noise(self):
        """
        Calculate noise level in spectrum. We assume the noise is homogenous across the
        spectrum. We estimate it by taking a region outside of the filter and calculating
        the standard deviation in that region. We use the normalized spectrum since that
        is what is passed to the fit function.
        """
        # Determine filter
        global bound_lower, bound_upper
        if self.filter == 'SN3':
            bound_lower = 14300#16000
            bound_upper = 14500#16400
        elif self.filter == 'SN2':
            bound_lower = 18600
            bound_upper = 19000
        elif self.filter == 'SN1':
            bound_lower = 25300
            bound_upper = 25700
        elif self.filter == 'C4' and 'Halpha' in self.lines:
            ## This is true for objects at redshift ~0.25
            # In this case we pretend we are in SN3
            bound_lower = 14300
            bound_upper = 14500
        else:
            print('The filter of your datacube is not supported by LUCI. We only support SN1, SN2, and SN3 at the moment.')
        # Calculate standard deviation
        min_ = np.argmin(np.abs(np.array(self.axis)-bound_lower))
        max_ = np.argmin(np.abs(np.array(self.axis)-bound_upper))
        spec_noise = self.spectrum_clean[min_:max_]
        self.noise = np.nanstd(spec_noise)


    def estimate_priors_ML(self, mdn=True):
        """
        Apply machine learning algorithm on spectrum in order to estimate the velocity.
        The spectrum fed into this method must be interpolated already onto the
        reference spectrum axis AND normalized as described in Rhea et al. 2020a.
        Args:
            mdn: Boolean to use MDN or not (default True)

        Return:
            Updates self.vel_ml
        """
        Spectrum = self.spectrum_interp_norm.reshape(1, self.spectrum_interp_norm.shape[0], 1)
        if self.mdn == True:
            prediction_distribution = self.ML_model(Spectrum, training=False)
            prediction_mean = prediction_distribution.mean().numpy().tolist()
            prediction_stdv = prediction_distribution.stddev().numpy().tolist()
            self.vel_ml = [pred[0] for pred in prediction_mean][0]
            self.vel_ml_sigma = [pred[0] for pred in prediction_stdv][0]
            self.broad_ml = [pred[1] for pred in prediction_mean][0]
            self.broad_ml_sigma = [pred[1] for pred in prediction_stdv][0]
        elif self.mdn == False:
            predictions = self.ML_model(Spectrum, training=False)
            self.vel_ml = float(predictions[0][0])
            self.vel_ml_sigma = 0
            self.broad_ml = float(predictions[0][1])
            self.broad_ml_sigma = 0
        return None


    def interpolate_spectrum(self):
        """
        Interpolate Spectrum given the wavelength axis of reference spectrum.
        Then normalize the spectrum so that the max value equals 1

        Return:
            Populates self.spectrum_interpolated, self.spectrum_scale, and self.spectrum_interp_norm.

        """
        self.spectrum_scale = np.max(self.spectrum)
        f = interpolate.interp1d(self.axis, self.spectrum, kind='slinear')
        self.spectrum_interpolated = f(self.wavenumbers_syn)
        self.spectrum_interp_scale = np.max(self.spectrum_interpolated)
        self.spectrum_interp_norm = self.spectrum_interpolated / self.spectrum_interp_scale
        return None

    def line_vals_estimate(self, line_name):
        """
        TODO: Test

        Function to estimate the position and amplitude of a given line.

        Args:
            line_name: Name of model. Available options are 'Halpha', 'NII6548', 'NII6543', 'SII6716', 'SII6731'

        Return:
            Estimated line amplitude in units of cm-1 (line_amp_est) and estimate line position in units of cm-1 (line_pos_est)

        """
        line_theo = self.line_dict[line_name]
        if self.ML_model is None or self.model_type == '':
            max_flux = np.argmax(self.spectrum_normalized)
            self.vel_ml = np.abs(3e5 * ((1e7/self.axis[max_flux] - line_theo) / line_theo))
            self.broad_ml = 10.0  # Best for now
        else:
            pass  # vel_ml and broad_ml already set using ML algorithm
        line_pos_est = 1e7 / ((self.vel_ml / 3e5) * line_theo + line_theo)  # Estimate of position of line in cm-1
        line_ind = np.argmin(np.abs(np.array(self.axis) - line_pos_est))
        try:
            line_amp_est = np.max([self.spectrum_normalized[line_ind - 4], self.spectrum_normalized[line_ind - 3],
                                   self.spectrum_normalized[line_ind - 2], self.spectrum_normalized[line_ind - 1],
                                   self.spectrum_normalized[line_ind],
                                   self.spectrum_normalized[line_ind + 1], self.spectrum_normalized[line_ind + 2],
                                   self.spectrum_normalized[line_ind + 3], self.spectrum_normalized[line_ind + 4]
                                   ])
        except:
            line_amp_est = self.spectrum_normalized[line_ind]
        line_broad_est = (line_pos_est * self.broad_ml) / (3e5)
        if self.mdn == True:
            # Update position and sigma_gauss bounds
            self.x_min = 1e7 / (((self.vel_ml+3*self.vel_ml_sigma) / 3e5) * line_theo + line_theo)  # Estimate of position of line in cm-1
            self.x_max = 1e7/(((self.vel_ml-3*self.vel_ml_sigma) / 3e5) * line_theo + line_theo)  # Estimate of position of line in cm-1
            self.sigma_min =  (line_pos_est * (self.broad_ml)) / (3e5) - 3*(line_pos_est * (self.broad_ml_sigma)) / (3e5)
            self.sigma_max =  (line_pos_est * (self.broad_ml)) / (3e5) + 3*(line_pos_est * (self.broad_ml_sigma)) / (3e5)
        return line_amp_est, line_pos_est, line_broad_est


    def cont_estimate(self, sigma_level):
        """
        TODO: Test

        Function to estimate the continuum level. We use a sigma clipping algorithm over the
        restricted axis/spectrum to effectively ignore emission lines. Therefore, we
        are left with the continuum. We take the min value of this continuum as the initial
        guess.

        Args:
            sigma_level: Sigma level to clip (Default=1)

        Return:
            Initial guess for continuum

        """
        # Clip values at given sigma level (defined by sigma_level)
        clipped_spec = astrostats.sigma_clip(self.spectrum_restricted, sigma=sigma_level, masked=False, copy=False, maxiters=3)
        # Now take the mean value to serve as the continuum value
        cont_val = np.min(clipped_spec)
        return cont_val



    def log_likelihood(self, theta):
        """
        Calculate log likelihood function evaluated given parameters on spectral axis

        Args:
            theta - List of parameters for all the models in the following order
                            [amplitude, line location, sigma, continuum constant]
                    The continuum constant is always the last argument regardless of the number of lines being modeled
            yerr: Error on Spectrum's flux values (default 1e-2)
        Return:
            Value of log likelihood

        """
        global model
        if self.model_type == 'gaussian':
            model = Gaussian().evaluate(self.axis_restricted, theta, self.line_num)
        elif self.model_type == 'sinc':
            model = Sinc().evaluate(self.axis_restricted, theta, self.line_num, self.sinc_width)
        elif self.model_type == 'sincgauss':
            model = SincGauss().evaluate(self.axis_restricted, theta, self.line_num, self.sinc_width)
        # Add constant contimuum to model
        model += theta[-1]
        sigma2 = self.noise ** 2
        return -0.5 * np.sum((self.spectrum_restricted - model) ** 2 / sigma2 + np.log(2 * np.pi * sigma2))

    def fun_der(self, theta, yerr):
        return Jacobian(lambda theta: self.log_likelihood(theta, yerr))(theta).ravel()


    def sigma_constraints(self):
        """
        Set up constraints for sigma values before fitting line
        Return:
            Dictionary describing constraints
        """
        sigma_dict_list = []
        unique_rels = np.unique(self.sigma_rel)  # List of unique groups
        for unique_ in unique_rels:  # Step through each unique group
            inds_unique = [i for i, e in enumerate(self.sigma_rel) if e == unique_]  # Obtain line indices in group
            if len(inds_unique) > 1:  # If there is more than one element in the group
                ind_0 = inds_unique[0]  # Get first element
                for ind_unique in inds_unique[1:]:  # Step through group elements except for the first one
                    sigma_dict_list.append({'type': 'eq', 'fun': lambda x: x[3*ind_0+2] - x[3*ind_unique+2]})
        return sigma_dict_list

    def vel_constraints(self):
        """
        Set up constraints for velocity values before fitting line
        Return:
            Dictionary describing constraints
        """
        vel_dict_list = []
        unique_rels = np.unique(self.vel_rel)  # List of unique groups
        for unique_ in unique_rels:  # Step through each unique group
            inds_unique = [i for i, e in enumerate(self.vel_rel) if e == unique_]  # Obtain line indices in group
            if len(inds_unique) > 1:  # If there is more than one element in the group
                ind_0 = inds_unique[0]  # Get first element
                for ind_unique in inds_unique[1:]:  # Step through group elements except for the first one
                    expr_dict = {'type': 'eq',
                             'fun': lambda x: 3e5 * ((1e7 / x[3*ind_unique+1] - self.line_dict.values()[3*ind_unique+1]) / (1e7 / x[3*ind_unique+1])) - 3e5 * (
                                     (1e7 / x[3*ind_0+1] - self.line_dict.values()[3*ind_0+1]) / (1e7 / x[3*ind_0+1]))}
        return vel_dict_list


    def multiple_component_vel_constraint(self):
        """
        Constraints for the case that we have multiple components.
        If there are two components (i.e. the user passes the same line twice),
        we require that the first component has a higher velocity than the second component.
        This forces the solver to find the two components instead of simply fitting the same
        component twice.
        """
        vel_dict_list = []
        unique_rels = np.unique(self.lines)  # List of unique groups
        for unique_ in unique_rels:  # Step through each unique group
            inds_unique = [i for i, e in enumerate(self.lines) if e == unique_]  # Obtain line indices in group
            if len(inds_unique) > 1:  # If there is more than one element in the group
                ind_0 = inds_unique[0]  # Get first element
                for ind_unique in inds_unique[1:]:  # Step through group elements except for the first one
                    expr_dict = {'type': 'ineq', 'fun': lambda x: x[3*ind_unique+1] - x[3*ind_0+1] - 10}
        return vel_dict_list


    def calculate_params(self):
        """
        Calculate the amplitude, position, and sigma of the line. These values are
        calculated using the scipy.optimize.minimize function. This is called
        on the log likelood previously described. The minimization algorithm uses
        the SLSQP optimization implementation. We have applied standard bounds in order
        to speed up the fitting. We also apply the fit on the normalized spectrum.
        We then correct the flux by un-normalizing the spectrum.

        """
        nll = lambda *args: -self.log_likelihood(*args)
        initial = np.ones((3 * self.line_num + 1))
        bounds_ = []
        initial[-1] = self.cont_estimate(sigma_level=2.0)  # Add continuum constant and intialize it
        for mod in range(self.line_num):
            #val = 3 * mod + 1
            amp_est, vel_est, sigma_est = self.line_vals_estimate(self.lines[mod])
            initial[3 * mod] = amp_est - initial[-1]
            initial[3 * mod + 1] = vel_est
            initial[3 * mod + 2] = sigma_est
            bounds_.append((self.A_min, self.A_max))
            bounds_.append((self.x_min, self.x_max))
            bounds_.append((self.sigma_min, self.sigma_max))
        bounds_l = [val[0] for val in bounds_] + [0.0]  # Continuum Constraint
        bounds_u = [val[1] for val in bounds_] + [0.75]  # Continuum Constraint
        bounds = Bounds(bounds_l, bounds_u)
        self.inital_values = initial
        sigma_cons = self.sigma_constraints()
        vel_cons = self.vel_constraints()
        #vel_cons_multiple = self.multiple_component_vel_constraint()
        cons = (sigma_cons + vel_cons)# + vel_cons_multiple)
        soln = minimize(nll, initial, method='SLSQP', #method='SLSQP',# jac=self.fun_der(),
                        options={'disp': False, 'maxiter': 5000}, bounds=bounds, tol=1e-8,
                        args=(), constraints=cons)
        parameters = soln.x
        if self.uncertainty_bool == True:
            # Calculate uncertainties using the negative inverse hessian  as the covariance matrix
            try:
                hessian = Hessian(nll)
                hessian_calc = hessian(parameters)
                covariance_mat = -np.linalg.inv(hessian_calc)
                self.uncertainties = np.sqrt(np.abs(np.diagonal(covariance_mat)))
            except np.linalg.LinAlgError:
                self.uncertainties = np.zeros_like(parameters)
        # We now must unscale the amplitude
        for i in range(self.line_num):
            parameters[i * 3] *= self.spectrum_scale
            self.uncertainties[i*3] *= self.spectrum_scale
        # Scale continuum
        parameters[-1] *= self.spectrum_scale
        self.uncertainties[-1] *= self.spectrum_scale
        self.fit_sol = parameters
        if self.model_type == 'gaussian':
            self.fit_vector = Gaussian().plot(self.axis, self.fit_sol[:-1], self.line_num) + self.fit_sol[-1]
        elif self.model_type == 'sinc':
            self.fit_vector = Sinc().plot(self.axis, self.fit_sol[:-1], self.line_num, self.sinc_width) + self.fit_sol[-1]
        elif self.model_type == 'sincgauss':
            self.fit_vector = SincGauss().plot(self.axis, self.fit_sol[:-1], self.line_num, self.sinc_width) + self.fit_sol[-1]

        return None



    def fit(self):
        """
        Primary function call for a spectrum. This will estimate the velocity using
        our machine learning algorithm described in Rhea et al. 2020a. Then we will
        fit our lines using scipy.optimize.minimize.

        Return:
            dictionary of parameters returned by the fit. The dictionary has the following form:
            {"fit_vector": Fitted spectrum, "velocity": Velocity of the line in km/s (float),
            "broadening": Velocity Dispersion of the line in km/s (float)}
        """
        if self.ML_model != None:
            # Interpolate Spectrum
            self.interpolate_spectrum()
            # Estimate the priors using machine learning algorithm
            self.estimate_priors_ML()
        else:
            self.spectrum_scale = np.max(self.spectrum)
        # Apply Fit
        self.calculate_params()
        # Check if Bayesian approach is required
        if self.bayes_bool == True:
            self.fit_Bayes()
        # Calculate fit statistic
        chi_sqr, red_chi_sqr = self.calc_chisquare(self.fit_vector, self.spectrum, self.noise, 3*self.line_num+1)
        # Collect Amplitudes
        ampls = []
        fluxes = []
        vels = []
        sigmas = []
        vels_errors = []
        sigmas_errors = []
        flux_errors = []
        for line_ct, line_ in enumerate(self.lines):  # Step through each line
            ampls.append(self.fit_sol[line_ct * 3])
            # Calculate flux
            fluxes.append(calculate_flux(self.fit_sol[line_ct * 3], self.fit_sol[line_ct * 3 + 2], self.model_type, self.sinc_width))
            vels.append(calculate_vel(line_ct, self.lines, self.fit_sol, self.line_dict))
            sigmas.append(calculate_broad(line_ct, self.fit_sol, self.axis_step))
            vels_errors.append(calculate_vel_err(line_ct,  self.lines, self.fit_sol, self.line_dict, self.uncertainties))
            sigmas_errors.append(calculate_broad_err(line_ct, self.fit_sol, self.axis_step, self.uncertainties))
            flux_errors.append(calculate_flux_err(line_ct, self.fit_sol, self.uncertainties, self.model_type, self.sinc_width))
        # Collect parameters to return in a dictionary
        fit_dict = {'fit_sol': self.fit_sol, 'fit_uncertainties': self.uncertainties,
                    'fit_vector': self.fit_vector, 'fit_axis':self.axis,
                    'amplitudes': ampls, 'fluxes': fluxes, 'flux_errors': flux_errors, 'chi2': red_chi_sqr,
                    'velocities': vels, 'sigmas': sigmas,
                    'vels_errors': vels_errors, 'sigmas_errors': sigmas_errors,
                    'axis_step': self.axis_step, 'corr': self.correction_factor,
                    'continuum': self.fit_sol[-1], 'scale':self.spectrum_scale}
        return fit_dict

    def fit_Bayes(self):
        """
        Apply Bayesian MCMC run to constrain the parameters after solving
        """
        # Unscale the amplitude
        for i in range(self.line_num):
            self.fit_sol[i * 3] /= self.spectrum_scale
        self.fit_sol[-1] /= self.spectrum_scale
        # Set the number of dimensions -- this is somewhat arbitrary
        n_dim = 3 * self.line_num + 1
        # Set number of MCMC walkers. Again, this is somewhat arbitrary
        n_walkers = n_dim * 3 + 4
        # Initialize walkers
        random_ = 1e-4 * np.random.randn(n_walkers, n_dim)
        # Scale some of the walkers based on more realistic values
        #for i in range(self.line_num):
        #    random_[3*i] *= 0.05
        #    random_[3*i+2] *= 0.1
        #random_[-1] *= 0.01
        init_ = self.fit_sol  + random_ #+ self.fit_sol[-1] + random_
        # Ensure that walkers for amplitude and Gaussian broadening are positive
        #for i in range(self.line_num):
        #    init_[:,3*i] = np.abs(init_[:,3*i])
        #    init_[:,3*i+2] = np.abs(init_[:,3*i+2])
        #    print(init_[:, 3*i])
        # Ensure continuum values for walkers are positive
        init_[:,-1] = np.abs(init_[:,-1])
        # Set Ensemble Sampler
        sampler = emcee.EnsembleSampler(n_walkers, n_dim, log_probability,
                                        args=(self.axis_restricted, self.spectrum_restricted,
                                        self.noise,self.model_type, self.line_num, self.sinc_width,
                                        [self.vel_ml, self.broad_ml, self.vel_ml_sigma, self.broad_ml_sigma]
                                        )  # End additional args
                                        )  # End EnsembleSampler
        # Call Ensemble Sampler setting 2000 walks
        sampler.run_mcmc(init_, 2000, progress=True)
        # Obtain Ensemble Sampler results and discard first 200 walks (10%)
        flat_samples = sampler.get_chain(discard=200, flat=True)
        parameters_med = []
        parameters_std = []
        for i in range(n_dim):  # Calculate and store these results
            median = np.median(flat_samples[:, i])
            std = np.std(flat_samples[:, i])
            parameters_med.append(median)
            parameters_std.append(std)
        self.fit_sol = parameters_med
        self.uncertainties = parameters_std
        # We now must unscale the amplitude
        for i in range(self.line_num):
            parameters_med[i * 3] *= self.spectrum_scale
            self.uncertainties[i*3] *= self.spectrum_scale
        # Scale continuum
        parameters_med[-1] *= self.spectrum_scale
        self.uncertainties[-1] *= self.spectrum_scale
        self.fit_sol = parameters_med
        # Calculate fit vector using updated values
        if self.model_type == 'gaussian':
            self.fit_vector = Gaussian().plot(self.axis, self.fit_sol[:-1], self.line_num) + self.fit_sol[-1]
        elif self.model_type == 'sinc':
            self.fit_vector = Sinc().plot(self.axis, self.fit_sol[:-1], self.line_num, self.sinc_width) + self.fit_sol[-1]
        elif self.model_type == 'sincgauss':
            self.fit_vector = SincGauss().plot(self.axis, self.fit_sol[:-1], self.line_num, self.sinc_width) + self.fit_sol[-1]

    def calc_chisquare(self, fit_vector, init_spectrum, init_errors, n_dof):
        """
        Calculate reduced chi 2

        Args:
            fit_vector: Spectrum obtained from fit
            init_spectrum: Observed spectrum
            init_errors: Errors on observed spectrum
            n_dof: Number of degrees of freedom

        Return:
            chi2: Chi squared value
            chi2dof: Reduced chi squared value
        """
        # compute the mean and the chi^2/dof
        min_restricted, max_restricted = self.restrict_wavelength()
        z = (fit_vector[min_restricted: max_restricted] - init_spectrum[min_restricted: max_restricted]) / init_spectrum[min_restricted: max_restricted]
        chi2 = np.sum((z ** 2))#/(self.spectrum_scale))
        chi2dof = chi2 / (n_dof - 1)
        return chi2, chi2dof



    def check_lines(self):
        """
        This function checks to see that the lines provided are in the available options
        Return:
        Nothing if the user provides appropriate lines
        Else it will throw an error

        """
        if set(self.lines).issubset(self.line_dict):
            pass
        else:
            raise Exception('Please submit a line name in the available list: \n {}'.format(self.line_dict.keys()))

    def check_fitting_model(self):
        """
        This function checks to see that the model provided is in the available options
        Return:
        Nothing if the user provides an appropriate fitting model
        Else it will throw an error

        """
        if self.model_type in self.available_functions:
            pass
        else:
            raise Exception(
                'Please submit a fitting function name in the available list: \n {}'.format(self.available_functions))


    def check_lengths(self):
        """
        This function checks to see that the length of the sigma_rel and vel_rel arguments are correct
        Return:
        Nothing if the user provides appropriate length
        Else it will throw an error

        """
        if len(self.vel_rel) != len(self.lines):
            raise Exception("The argument vel_rel has %i arguments, but it should have %i arguments"%(len(self.vel_rel), len(self.lines)))
        elif len(self.sigma_rel) != len(self.lines):
            raise Exception("The argument sigma_rel has %i arguments, but it should have %i arguments"%(len(self.sigma_rel), len(self.lines)))
        else:
            pass
