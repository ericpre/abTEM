"""Module for various convenient utilities."""
import numpy as np
from ase import units

from abtem.device import get_array_module
from tqdm.auto import tqdm


def energy2mass(energy):
    """
    Calculate relativistic mass from energy.

    Parameters
    ----------
    energy: float
        Energy [eV].

    Returns
    -------
    float
        Relativistic mass [kg]̄
    """

    return (1 + units._e * energy / (units._me * units._c ** 2)) * units._me


def energy2wavelength(energy):
    """
    Calculate relativistic de Broglie wavelength from energy.

    Parameters
    ----------
    energy: float
        Energy [eV].

    Returns
    -------
    float
        Relativistic de Broglie wavelength [Å].
    """

    return units._hplanck * units._c / np.sqrt(
        energy * (2 * units._me * units._c ** 2 / units._e + energy)) / units._e * 1.e10


def energy2sigma(energy):
    """
    Calculate interaction parameter from energy.

    Parameters
    ----------
    energy: float
        Energy [ev].

    Returns
    -------
    float
        Interaction parameter [1 / (Å * eV)].
    """

    return (2 * np.pi * energy2mass(energy) * units.kg * units._e * units.C * energy2wavelength(energy) / (
            units._hplanck * units.s * units.J) ** 2)


def spatial_frequencies(gpts, sampling):
    """
    Calculate spatial frequencies of a grid.

    Parameters
    ----------
    gpts: tuple of int
        Number of grid points.
    sampling: tuple of float
        Sampling of the potential [1 / Å].

    Returns
    -------
    tuple of arrays
    """

    return tuple(np.fft.fftfreq(n, d).astype(np.float32) for n, d in zip(gpts, sampling))


def polar_coordinates(x, y):
    """Calculate a polar grid for a given Cartesian grid."""
    xp = get_array_module(x)
    alpha = xp.sqrt(x.reshape((-1, 1)) ** 2 + y.reshape((1, -1)) ** 2)
    phi = xp.arctan2(x.reshape((-1, 1)), y.reshape((1, -1)))
    return alpha, phi


def fft_interpolation_masks(shape1, shape2, xp=np, epsilon=1e-7):
    kx1 = xp.fft.fftfreq(shape1[0], 1 / shape1[0])
    ky1 = xp.fft.fftfreq(shape1[1], 1 / shape1[1])

    kx2 = xp.fft.fftfreq(shape2[0], 1 / shape2[0])
    ky2 = xp.fft.fftfreq(shape2[1], 1 / shape2[1])

    kx_min = max(xp.min(kx1), xp.min(kx2)) - epsilon
    kx_max = min(xp.max(kx1), xp.max(kx2)) + epsilon
    ky_min = max(xp.min(ky1), xp.min(ky2)) - epsilon
    ky_max = min(xp.max(ky1), xp.max(ky2)) + epsilon

    kx1, ky1 = xp.meshgrid(kx1, ky1, indexing='ij')
    kx2, ky2 = xp.meshgrid(kx2, ky2, indexing='ij')

    mask1 = (kx1 <= kx_max) & (kx1 >= kx_min) & (ky1 <= ky_max) & (ky1 >= ky_min)
    mask2 = (kx2 <= kx_max) & (kx2 >= kx_min) & (ky2 <= ky_max) & (ky2 >= ky_min)
    return mask1, mask2


# def is_points_in_box(points, box):

def periodic_crop(array, corners, new_shape):
    xp = get_array_module(array)

    if ((corners[0] > 0) &
            (corners[1] > 0) &
            (corners[0] + new_shape[0] < array.shape[-2]) &
            (corners[1] + new_shape[1] < array.shape[-1])):
        array = array[..., corners[0]:corners[0] + new_shape[0], corners[1]:corners[1] + new_shape[1]]
        return array

    x = xp.arange(corners[0], corners[0] + new_shape[0], dtype=xp.int) % array.shape[-2]
    y = xp.arange(corners[1], corners[1] + new_shape[1], dtype=xp.int) % array.shape[-1]

    x, y = xp.meshgrid(x, y, indexing='ij')
    array = array[..., x.ravel(), y.ravel()].reshape(array.shape[:-2] + new_shape)
    return array


def fft_crop(array, new_shape):
    assert np.iscomplexobj(array)
    xp = get_array_module(array)
    mask_in, mask_out = fft_interpolation_masks(array.shape, new_shape)
    new_array = xp.zeros(new_shape, dtype=np.complex64)
    new_array[..., mask_out] = array[..., mask_in]
    return new_array


def fft_interpolate_2d(array, new_shape):
    xp = get_array_module(array)
    if np.iscomplexobj(array):
        return xp.fft.ifft2(xp.fft.fft2(fft_crop(array, new_shape)))
    else:
        return xp.fft.ifft2(xp.fft.fft2(fft_crop(array, new_shape))).real


def split_integer(n: int, m: int):
    """
    Split an n integer into m (almost) equal integers, such that the sum of smaller integers equals n.

    Parameters
    ----------
    n: int
        The integer to split.
    m: int
        The number integers n will be split into.

    Returns
    -------
    list of int
    """

    if n < m:
        raise RuntimeError('n may not be larger than m')

    elif n % m == 0:
        return [n // m] * m
    else:
        v = []
        zp = m - (n % m)
        pp = n // m
        for i in range(m):
            if i >= zp:
                v = [pp + 1] + v
            else:
                v = [pp] + v
        return v


class ProgressBar:
    """Object to describe progress bar indicators for computations."""

    def __init__(self, **kwargs):
        self._tqdm = tqdm(**kwargs)

    @property
    def tqdm(self):
        return self._tqdm

    @property
    def disable(self):
        return self.tqdm.disable

    def update(self, n):
        if not self.disable:
            self.tqdm.update(n)

    def reset(self):
        if not self.disable:
            self.tqdm.reset()

    def refresh(self):
        if not self.disable:
            self.tqdm.refresh()

    def close(self):
        self.tqdm.close()
