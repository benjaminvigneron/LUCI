"""
Suite of simulations tools in LUCI
"""

import numpy as np
from LuciFunctions import Gaussian, Sinc, SincGauss

def create_spectrum(lines, fit_function, ampls, velocity, broadening, delta_x, n_steps, order, theta):
    """
    Create a mock spectrum given the lines of interest, the fitting function, the
    amplitudes of the lines, the kinematics of the lines, and the interferometer parameters.
    The interferometer parameters (step, n_steps, order & theta) will be used to construct the x-axis.

    Args:
        lines: List of lines to fit
        fit_function: Fit function (options: 'gaussian', 'sinc', 'sincgauss')
        ampls: List of line amplitudes
        velocity: Velocity of lines
        broadening: Broadening of lines
        delta_x: Step size in nm  ['STEP'] in the header
        n_steps: Number of steps in spectra
        order: Order of folding
        theta: Interferometric Angle
    """
    # Calculate correction factor
    corr = 1/np.cos(theta)
    # Construct x axis in units of cm-1
    x_min = order/(2*delta_x) * corr * 1e7
    x_max = (order+1)/(2*delta_x) * corr * 1e7
    axis = np.array([min_+j*step_/n_steps for j in range(n_steps)])
