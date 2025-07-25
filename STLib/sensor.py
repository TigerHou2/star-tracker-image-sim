import numpy as np
from .lens import Lens

from scipy.signal import convolve2d

from astropy import units

from numpy.typing import ArrayLike
from typing import Union
from collections.abc import Callable
from .utils import timer, type_checker, Number

# create a new equivalency where 1 pA = 6.28e6 e-/s
electron_current_density = [(units.pA/units.m**2, units.electron/units.s/units.m**2, lambda x: x * 6.28e6, lambda x: x / 6.28e6)]

class Sensor:

    @type_checker
    def __init__(self, *, 
                 width_px               : int, 
                 height_px              : int, 
                 px_len                 : Union[Number, tuple[Number, Number]], 
                 px_pitch               : Union[Number, tuple[Number, Number]], 
                 quantum_efficiency     : Number, 
                 dark_current           : Callable[[Number], float], 
                 hot_pixels             : Union[None, ArrayLike],
                 read_noise             : Number,
                 gain                   : Number, 
                 bias                   : Union[int, ArrayLike], 
                 full_well_capacity     : Number, 
                 adc_limit              : int,
                 bloom                  : set, 
                 readout_time           : Number):
        
        self.width_px  = int(width_px)
        self.height_px = int(height_px)
        self.pixels    = np.zeros((self.height_px, self.width_px)) * units.electron

        if isinstance(px_len, tuple):
            self.px_len_x = float(px_len[0]) * units.micron
            self.px_len_y = float(px_len[1]) * units.micron
            self.px_area = self.px_len_x * self.px_len_y
        else:
            self.px_len_x = float(px_len) * units.micron
            self.px_len_y = float(px_len) * units.micron
            self.px_area = self.px_len_x * self.px_len_y

        if isinstance(px_pitch, tuple):
            self.px_pitch_x = float(px_pitch[0]) * units.micron
            self.px_pitch_y = float(px_pitch[1]) * units.micron
        else:
            self.px_pitch_x = float(px_pitch) * units.micron
            self.px_pitch_y = float(px_pitch) * units.micron

        self.width  = (self.width_px +1) * self.px_pitch_x - self.px_len_x
        self.height = (self.height_px+1) * self.px_pitch_y - self.px_len_y

        self.quantum_eff    = float(quantum_efficiency)
    
        self.dark_current   = dark_current  # pA / cm**2
        
        if hot_pixels is None:
            self.hot_pixels = np.ones_like(self.pixels.value)
        else:
            self.hot_pixels = np.asarray(hot_pixels)
            if self.hot_pixels.shape != (self.height_px, self.width_px):
                raise ValueError("Argument `hot_pixels` must be None or 2D array_like with shape (`height_px`, `width_px`).")

        self.read_noise     = float(read_noise) * units.electron
        self.gain           = float(gain) * units.adu / units.electron

        bias = np.asanyarray(bias, dtype=int)
        _ERR_bias = ValueError("Argument `bias` must be an integer, 1D array_like with length `width_px`, or 2D array_like with shape (`height_px`, `width_px`).")
        if bias.ndim == 0:
            pass
        elif bias.ndim == 1:
            if len(bias) == 1:
                pass
            if len(bias) == self.width_px:
                bias = np.tile(bias, (self.height_px, 1))
            else:
                raise _ERR_bias
        elif bias.ndim == 2:
            if bias.shape != (self.height_px, self.width_px):
                raise _ERR_bias
        else:
            raise _ERR_bias
        self.bias = bias * units.adu

        self.full_well      = float(full_well_capacity) * units.electron
        self.adc_limit      = adc_limit * units.adu
        self.bloom          = bloom
        self.readout_time   = float(readout_time) * units.s

        if not self.bloom.issubset({'+x','-x','+y','-y'}):
            raise ValueError("Argument `bloom` must be a subset of {'+x','-x','+y','-y'}.")
        
    
    def clear(self):
        self.pixels = np.zeros((self.height_px, self.width_px)) * units.electron


    def accumulate(self, 
                   lens: Lens,
                   exposure_time : float,
                   temperature   : float,
                   xcoords       : ArrayLike,   # distance coordinates (relative to top left corner)
                   ycoords       : ArrayLike, 
                   photon_flux_density  : ArrayLike,
                   background_flux      : ArrayLike):
        
        exposure_time *= units.s
        
        # source flux

        if len(photon_flux_density) > 0:
            electron_dose = photon_flux_density * exposure_time * self.quantum_eff * lens.area
            self._applyPSF(lens, xcoords, ycoords, electron_dose)

        # sky background flux

        bg_electron_dose = background_flux * exposure_time * units.pixel * lens.area
        self.pixels += np.random.poisson(bg_electron_dose.to(units.electron).value, (self.height_px, self.width_px)) * units.electron

        # dark current

        dark_current = self.dark_current(temperature) * units.pA / units.cm**2
        dark_count = dark_current.to(units.electron/units.s/units.micron**2, equivalencies=electron_current_density) * exposure_time * self.px_area
        dark_lambda = self.hot_pixels * dark_count.to(units.electron).value
        self.pixels += np.random.poisson(dark_lambda) * units.electron
        
        # saturation and bloom

        self._applyBloom()


    def readout(self):

        # global shutter
            
        # read noise
        self.pixels += np.random.poisson(self.read_noise.to(units.electron).value, (self.height_px, self.width_px)) * units.electron

        # analog to digital conversion, clip to ADC limit
        return np.minimum(np.floor(self.pixels * self.gain) + self.bias, self.adc_limit)

        # TODO: implement rolling shutter?


    @timer
    def _applyPSF(self, 
                  lens          : Lens, 
                  xcoords       : ArrayLike, 
                  ycoords       : ArrayLike, 
                  electron_dose : ArrayLike):
        
        # for each pixel, at least sample four corners for integration
        nx = max(2, int(np.ceil((self.px_len_x / lens.psf_resolution).to(units.dimensionless_unscaled))))
        ny = max(2, int(np.ceil((self.px_len_y / lens.psf_resolution).to(units.dimensionless_unscaled))))
        dx = (self.px_len_x / (nx-1)).to_value(units.micron)
        dy = (self.px_len_y / (ny-1)).to_value(units.micron)
        
        # how many pixels in the ±x/y directions to do integration, 
        # relative to the pixel containing of center of the light source
        px_bounds_x = int(np.ceil((lens.psf_bounds_x / self.px_len_x).to(units.dimensionless_unscaled).value))
        px_bounds_y = int(np.ceil((lens.psf_bounds_y / self.px_len_y).to(units.dimensionless_unscaled).value))

        for x, y, i in zip(xcoords, ycoords, electron_dose):

            # find the center pixel

            xi = int(np.round(x / (self.width  - 2*self.px_pitch_x + self.px_len_x) * self.width_px ).to(units.dimensionless_unscaled).value)
            yi = int(np.round(y / (self.height - 2*self.px_pitch_y + self.px_len_y) * self.height_px).to(units.dimensionless_unscaled).value)

            # integrate PSF over each pixel

            xmin = max(xi - px_bounds_x, 0)
            ymin = max(yi - px_bounds_y, 0)
            xmax = min(xi + px_bounds_x + 1, self.width_px)  # + 1 because range doesn't include end
            ymax = min(yi + px_bounds_y + 1, self.height_px)

            idxs, idys = np.meshgrid(np.arange(xmin,xmax), np.arange(ymin,ymax))
            x_lb = ((self.px_pitch_x * np.ravel(idxs) - self.px_len_x) - x).to(units.micron).value
            x_ub = ((self.px_pitch_x * np.ravel(idxs))                 - x).to(units.micron).value
            y_lb = ((self.px_pitch_y * np.ravel(idys) - self.px_len_y) - y).to(units.micron).value
            y_ub = ((self.px_pitch_y * np.ravel(idys))                 - y).to(units.micron).value

            _x = np.linspace(x_lb, x_ub, nx).T
            _y = np.linspace(y_lb, y_ub, ny).T
            m = idxs.size
            # apply meshgrid to each row of _x and _y to form 3D tensor. Each slice represents one pixel.
            X = np.broadcast_to(_x[:, None, :], (m, ny, nx))
            Y = np.broadcast_to(_y[:, :, None], (m, ny, nx))

            count = i * np.trapezoid(np.trapezoid(lens.psf(X,Y),dx=dy,axis=1),dx=dx,axis=1)
            self.pixels[np.ravel(idys),np.ravel(idxs)] += np.random.poisson(count.to(units.electron).value) * units.electron
                    
        return
        

    @timer
    def _applyBloom(self):

        if not self.bloom:
            self.pixels = np.minimum(self.pixels, self.full_well)
            return
        
        conv_filter = np.zeros((3,3))
        frac = 1.0 / len(self.bloom)
        conv_filter[1,0] = frac if '-x' in self.bloom else 0
        conv_filter[1,2] = frac if '+x' in self.bloom else 0
        conv_filter[0,1] = frac if '-y' in self.bloom else 0
        conv_filter[2,1] = frac if '+y' in self.bloom else 0
        while True:
            excess = np.maximum(0, self.pixels - self.full_well)
            if not any(excess.flatten().value > 0):
                break
            self.pixels -= excess
            self.pixels += np.floor(convolve2d(excess.to(units.electron).value, conv_filter, mode='same', boundary='fill', fillvalue=0)) * units.electron
        
        return
    