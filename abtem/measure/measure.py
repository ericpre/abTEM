import copy
from abc import ABCMeta, abstractmethod
from numbers import Number
from typing import Union, Tuple, TypeVar, Dict, List, Sequence

import dask
import dask.array as da
import matplotlib.pyplot as plt
import numpy as np
import zarr
from ase import Atom
from scipy.interpolate import interp1d

from abtem.core.axes import HasAxes, RealSpaceAxis, AxisMetadata, FourierSpaceAxis, LinearAxis, axis_to_dict, \
    axis_from_dict, OrdinalAxis
from abtem.core.backend import cp, asnumpy, get_array_module, get_ndimage_module, copy_to_device
from abtem.core.complex import abs2
from abtem.core.dask import HasDaskArray
from abtem.core.energy import Accelerator, HasAcceleratorMixin
from abtem.core.fft import fft2
from abtem.core.fft import fft2_interpolate
from abtem.core.grid import Grid
from abtem.core.interpolate import interpolate_bilinear
from abtem.measure.utils import polar_detector_bins, sum_run_length_encoded
from abtem.visualize.utils import domain_coloring, add_domain_coloring_cbar

if cp is not None:
    from abtem.core.cuda import sum_run_length_encoded as sum_run_length_encoded_cuda
else:
    sum_run_length_encoded_cuda = None
    interpolate_bilinear_cuda = None

T = TypeVar('T', bound='AbstractMeasurement')


def _to_hyperspy_axes_metadata(axes_metadata, shape):
    hyperspy_axes = []
    for metadata, n in zip(axes_metadata, shape):
        hyperspy_axes.append({'size': n})
        
        axes_mapping = {'sampling': 'scale',
                        'units': 'units',
                        'label': 'name',
                        'offset': 'offset'
                        }
        for attr, mapped_attr in axes_mapping.items():
            if hasattr(metadata, attr):
                hyperspy_axes[-1][mapped_attr] = getattr(metadata, attr)

    return hyperspy_axes


def from_zarr(url):
    with zarr.open(url, mode='r') as f:
        d = {}

        for key, value in f.attrs.items():
            if key == 'extra_axes_metadata':
                extra_axes_metadata = [axis_from_dict(d) for d in value]
            elif key == 'type':
                cls = globals()[value]
            else:
                d[key] = value

    array = da.from_zarr(url, component='array')
    return cls(array, extra_axes_metadata=extra_axes_metadata, **d)


def stack_measurements(measurements, axes_metadata):
    array = np.stack([measurement.array for measurement in measurements])
    cls = measurements[0].__class__
    d = measurements[0]._copy_as_dict(copy_array=False)
    d['array'] = array
    d['extra_axes_metadata'] = [axes_metadata] + d['extra_axes_metadata']
    return cls(**d)


class AbstractMeasurement(HasDaskArray, HasAxes, metaclass=ABCMeta):

    def __init__(self, array, extra_axes_metadata, metadata):
        self._array = array

        if extra_axes_metadata is None:
            extra_axes_metadata = []

        if metadata is None:
            metadata = {}

        self._extra_axes_metadata = extra_axes_metadata
        self._metadata = metadata

        super().__init__(array)

        self._check_axes_metadata()

    def scan_positions(self):
        positions = ()
        for n, metadata in zip(self.scan_shape, self.scan_axes_metadata):
            positions += (
                np.linspace(metadata.offset, metadata.offset + metadata.sampling * n, n, endpoint=metadata.endpoint),)
        return positions

    def scan_extent(self):
        extent = ()
        for n, metadata in zip(self.scan_shape, self.scan_axes_metadata):
            extent += (metadata.sampling * n,)
        return extent

    def squeeze(self):

        self._

    @property
    @abstractmethod
    def base_axes_metadata(self) -> list:
        pass

    @property
    def metadata(self) -> dict:
        return self._metadata

    @property
    def base_shape(self) -> Tuple[float, ...]:
        return self.shape[-self.num_base_axes:]

    @property
    def dimensions(self) -> int:
        return len(self._array.shape)

    def _validate_axes(self, axes):
        if isinstance(axes, Number):
            return (axes,)
        return axes

    def _check_is_base_axes(self, axes):
        axes = self._validate_axes(axes)
        return len(set(axes).intersection(self.base_axes)) > 0

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False

        if not self.shape == other.shape:
            return False

        for (key, value), (other_key, other_value) in zip(self._copy_as_dict(copy_array=False).items(),
                                                          other._copy_as_dict(copy_array=False).items()):
            if np.any(value != other_value):
                return False

        if not np.allclose(self.array, other.array):
            return False

        return True

    def check_is_compatible(self, other):
        if not isinstance(other, self.__class__):
            raise RuntimeError(f'Incompatible measurement types ({self.__class__} is not {other.__class__})')

        if self.shape != other.shape:
            raise RuntimeError()

        for (key, value), (other_key, other_value) in zip(self._copy_as_dict(copy_array=False).items(),
                                                          other._copy_as_dict(copy_array=False).items()):
            if np.any(value != other_value):
                raise RuntimeError(f'{key}, {other_key} {value} {other_value}')

    def relative_difference(self, other, min_relative_tol=0.):
        difference = self.subtract(other)

        xp = get_array_module(self.array)

        # if min_relative_tol > 0.:
        valid = xp.abs(self.array) >= min_relative_tol * self.array.max()
        difference._array[valid] /= self.array[valid]
        difference._array[valid == 0] = 0.
        # else:
        #    difference._array[:] /= self.array

        return difference

    def __sub__(self, other):
        return self.subtract(other)

    def __isub__(self, other):
        return self.add(other, in_place=True)

    def __add__(self, other):
        return self.add(other)

    def __iadd__(self, other):
        return self.add(other, in_place=True)

    def add(self, other, in_place: bool = False) -> 'T':
        self.check_is_compatible(other)

        if in_place:
            self.array[:] += other.array
            return self

        d = self._copy_as_dict(copy_array=False)
        d['array'] = self.array + other.array
        return self.__class__(**d)

    def subtract(self, other, in_place: bool = False) -> 'T':
        self.check_is_compatible(other)

        if in_place:
            self.array[:] -= other.array
            return self

        d = self._copy_as_dict(copy_array=False)
        d['array'] = self.array - other.array
        return self.__class__(**d)

    def mean(self, axes, **kwargs):
        return self._reduction(np.mean, axes=axes, **kwargs)

    def sum(self, axes, **kwargs):
        return self._reduction(np.sum, axes=axes, **kwargs)

    def std(self, axes, **kwargs):
        return self._reduction(np.std, axes=axes, **kwargs)

    def _reduction(self, reduction_func, axes, split_every=2):

        if self._check_is_base_axes(axes):
            raise RuntimeError('base axes cannot be reduced')

        extra_axes_metadata = copy.deepcopy(self._extra_axes_metadata)
        del extra_axes_metadata[axes]

        d = self._copy_as_dict(copy_array=False)
        d['array'] = reduction_func(self.array, axes)
        d['extra_axes_metadata'] = extra_axes_metadata
        return self.__class__(**d)

    def _get_measurements(self, items):
        if isinstance(items, (Number, slice)):
            items = (items,)
        # elif isinstance(items, ):
        #    items = (items,)

        removed_axes = []
        for i, item in enumerate(items):
            if isinstance(item, Number):
                removed_axes.append(i)

        if self._check_is_base_axes(removed_axes):
            raise RuntimeError('base axes cannot be indexed')

        axes = [element for i, element in enumerate(self.extra_axes_metadata) if not i in removed_axes]

        d = self._copy_as_dict(copy_array=False)
        d['array'] = self._array[items]
        d['extra_axes_metadata'] = axes
        return self.__class__(**d)

    def _apply_element_wise_func(self, func):
        d = self._copy_as_dict(copy_array=False)
        d['array'] = func(self.array)
        return self.__class__(**d)

    def __getitem__(self, items):
        return self._get_measurements(items)

    def to_zarr(self, url, compute=True, overwrite=False):
        with zarr.open(url, mode='w') as root:
            if self.is_lazy:
                array = self.array.to_zarr(url, compute=compute, component='array', overwrite=overwrite)
            else:
                array = zarr.save(url, array=self.array)

            d = self._copy_as_dict(copy_array=False)
            for key, value in d.items():
                if key == 'extra_axes_metadata':
                    root.attrs[key] = [axis_to_dict(axis) for axis in value]
                else:
                    root.attrs[key] = value

            root.attrs['type'] = self.__class__.__name__
        return array

    @staticmethod
    def from_zarr(url) -> 'T':
        return from_zarr(url)

    @abstractmethod
    def to_hyperspy(self):
        pass

    def to_cpu(self) -> 'T':
        d = self._copy_as_dict(copy_array=False)
        d['array'] = asnumpy(self.array)
        return self.__class__(**d)

    @abstractmethod
    def _copy_as_dict(self, copy_array: bool = True) -> dict:
        pass

    def copy(self) -> 'T':
        return self.__class__(**self._copy_as_dict())


class Images(AbstractMeasurement):

    def __init__(self,
                 array: Union[da.core.Array, np.array],
                 sampling: Union[float, Tuple[float, float]],
                 extra_axes_metadata: List[AxisMetadata] = None,
                 metadata: Dict = None):

        if isinstance(sampling, Number):
            sampling = (sampling,) * 2

        self._sampling = sampling
        super().__init__(array=array, extra_axes_metadata=extra_axes_metadata, metadata=metadata)

    @property
    def base_axes_metadata(self) -> List[AxisMetadata]:
        return [RealSpaceAxis(label='x', sampling=self.sampling[0], units='Å'),
                RealSpaceAxis(label='y', sampling=self.sampling[0], units='Å')]

    def _check_is_complex(self):
        if not np.iscomplexobj(self.array):
            raise RuntimeError('function not implemented for non-complex image')

    def angle(self):
        self._check_is_complex()
        return self._apply_element_wise_func(get_array_module(self.array).angle)

    def abs(self):
        self._check_is_complex()
        return self._apply_element_wise_func(get_array_module(self.array).abs)

    def intensity(self):
        self._check_is_complex()
        return self._apply_element_wise_func(abs2)

    def to_hyperspy(self):
        from hyperspy._signals.signal2d import Signal2D

        axes = [
            {'scale': self.sampling[1], 'units': 'Å', 'name': 'y', 'offset': 0., 'size': self.array.shape[1]},
            {'scale': self.sampling[0], 'units': 'Å', 'name': 'x', 'offset': 0., 'size': self.array.shape[0]}
        ]

        axes += [{'size': n} for n in self.array.shape[:-2]]

        return Signal2D(self.array.T, axes=axes)

    def crop(self, extent):
        new_shape = (np.round(self.base_shape[0] * extent[0] / self.extent[0]),
                     np.round(self.base_shape[1] * extent[1] / self.extent[1]))
        new_array = self.array[..., 0:new_shape[0], 0:new_shape[1]]

        d = self._copy_as_dict(copy_array=False)
        d['array'] = new_array
        return self.__class__(**d)

    def _copy_as_dict(self, copy_array: bool = True):
        d = {'sampling': self.sampling,
             'extra_axes_metadata': copy.deepcopy(self.extra_axes_metadata),
             'metadata': copy.deepcopy(self.metadata)}

        if copy_array:
            d['array'] = self.array.copy()
        return d

    @property
    def sampling(self) -> Tuple[float, float]:
        return self._sampling

    @property
    def coordinates(self) -> Tuple[np.ndarray, np.ndarray]:
        x = np.linspace(0., self.shape[-2] * self.sampling[0], self.shape[-2])
        y = np.linspace(0., self.shape[-1] * self.sampling[1], self.shape[-1])
        return x, y

    @property
    def extent(self) -> Tuple[float, float]:
        return (self.sampling[0] * self.base_shape[0], self.sampling[1] * self.base_shape[1])

    def interpolate(self,
                    sampling: Union[float, Tuple[float, float]] = None,
                    gpts: Union[int, Tuple[int, int]] = None,
                    method: str = 'fft',
                    boundary: str = 'periodic',
                    normalization='values') -> 'Images':

        if method == 'fft' and boundary != 'periodic':
            raise ValueError()

        if sampling is None and gpts is None:
            raise ValueError()

        if gpts is None and sampling is not None:
            if np.isscalar(sampling):
                sampling = (sampling,) * 2
            gpts = tuple(int(np.ceil(l / d)) for d, l in zip(sampling, self.extent))
        elif gpts is not None:
            if np.isscalar(gpts):
                gpts = (gpts,) * 2
        else:
            raise ValueError()

        xp = get_array_module(self.array)

        sampling = (self.extent[0] / gpts[0], self.extent[1] / gpts[1])

        if self.is_lazy:
            array = dask.delayed(fft2_interpolate)(self.array, gpts, normalization=normalization)
            array = da.from_delayed(array, shape=self.shape[:-2] + gpts, meta=xp.array((), dtype=self.array.dtype))
        else:
            array = fft2_interpolate(self.array, gpts, normalization=normalization)

        d = self._copy_as_dict(copy_array=False)
        d['sampling'] = sampling
        d['array'] = array
        return self.__class__(**d)

    def interpolate_line(self,
                         start: Union[Tuple[float, float], Atom],
                         end: Union[Tuple[float, float], Atom] = None,
                         angle: float = None,
                         gpts: int = None,
                         sampling: float = None,
                         width: float = None,
                         margin: float = 0.,
                         interpolation_method: str = 'splinef2d') -> 'LineProfiles':
        """
        Interpolate 2d measurement along a line.

        Parameters
        ----------
        start : two float, Atom
            Start point on line [Å].
        end : two float, Atom, optional
            End point on line [Å].
        angle : float, optional
            The angle of the line. This is only used when an "end" is not give.
        gpts : int
            Number of grid points along line.
        sampling : float
            Sampling rate of grid points along line [1 / Å].
        width : float, optional
            The interpolation will be averaged across line of this width.
        margin : float, optional
            The line will be extended by this amount at both ends.
        interpolation_method : str, optional
            The interpolation method.

        Returns
        -------
        Measurement
            Line profile measurement.
        """

        # if (gpts is None) & (sampling is None):
        #    sampling = (measurement.calibrations[0].sampling + measurement.calibrations[1].sampling) / 2.

        from abtem.waves.scan import LineScan

        if (sampling is None) and (gpts is None):
            sampling = min(self.sampling)

        xp = get_array_module(self.array)

        scan = LineScan(start=start, end=end, angle=angle, gpts=gpts, sampling=sampling, margin=margin)
        positions = xp.asarray(scan.get_positions() / self.sampling)

        from scipy.ndimage import map_coordinates

        def interpolate_1d_from_2d(array, positions):
            old_shape = array.shape
            array = array.reshape((-1,) + array.shape[-2:])
            output = xp.zeros((array.shape[0], positions.shape[0]))

            for i in range(array.shape[0]):
                map_coordinates(array[i], positions.T, output=output[i], mode='wrap')

            output = output.reshape(old_shape[:-2] + (output.shape[-1],))
            return output

        # array = self.array.map_blocks(interpolate_1d_from_2d,
        #                               positions=positions,
        #                               drop_axis=(self.num_ensemble_axes, self.num_ensemble_axes + 1),
        #                               chunks=self.array.chunks[:-2] + ((positions.shape[0],),),
        #                               new_axis=(self.num_ensemble_axes,),
        #                               meta=np.array((), dtype=np.float32))

        padded_array = xp.pad(self.array, (5,) * 2, mode='wrap')
        array = interpolate_1d_from_2d(padded_array, positions=positions + 5)

        return LineProfiles(array=array, start=scan.start, end=scan.end, extra_axes_metadata=self.extra_axes_metadata)

    def tile(self, reps: Tuple[int, int]) -> 'Images':
        if len(reps) != 2:
            raise RuntimeError()
        d = self._copy_as_dict(copy_array=False)
        d['array'] = np.tile(self.array, (1,) * (len(self.array.shape) - 2) + reps)
        return self.__class__(**d)

    def gaussian_filter(self, sigma: Union[float, Tuple[float, float]], boundary: str = 'periodic'):
        xp = get_array_module(self.array)
        ndimage = get_ndimage_module(self._array)

        gaussian_filter = ndimage.gaussian_filter

        if np.isscalar(sigma):
            sigma = (sigma,) * 2

        sigma = (0,) * (len(self.shape) - 2) + tuple(s / d for s, d in zip(sigma, self.sampling))

        array = self.array.map_overlap(gaussian_filter,
                                       sigma=sigma,
                                       boundary=boundary,
                                       depth=(0,) * (len(self.shape) - 2) + (int(np.ceil(4.0 * max(sigma))),) * 2,
                                       meta=xp.array((), dtype=xp.float32))

        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        return self.__class__(**d)

    def diffractograms(self) -> 'DiffractionPatterns':
        array = fft2(self.array)
        array = np.fft.fftshift(np.abs(array), axes=(-2, -1))
        # wavelength = energy2wavelength(self.metadata['energy'])
        # angular_sampling = 1 / self.extent[0] * wavelength * 1e3, 1 / self.extent[1] * wavelength * 1e3
        angular_sampling = 1 / self.extent[0], 1 / self.extent[1]
        return DiffractionPatterns(array=array,
                                   angular_sampling=angular_sampling,
                                   extra_axes_metadata=self.extra_axes_metadata,
                                   metadata=self.metadata)

    def show(self, ax=None, cbar: bool = False, power: float = 1., figsize: Tuple[int, int] = None, title: str = None,
             vmin=None, vmax=None, **kwargs):

        self.compute(pbar=False)

        if ax is None:
            fig, ax = plt.subplots(figsize=figsize)

        ax.set_title(title)
        array = asnumpy(self._array)[(0,) * self.num_extra_axes].T ** power

        if np.iscomplexobj(array):
            colored_array = domain_coloring(array, vmin=vmin, vmax=vmax)
        else:
            colored_array = array

        im = ax.imshow(colored_array, extent=[0, self.extent[0], 0, self.extent[1]], origin='lower', vmin=vmin,
                       vmax=vmax, **kwargs)
        ax.set_xlabel('x [Å]')
        ax.set_ylabel('y [Å]')

        if cbar:
            if np.iscomplexobj(array):
                vmin = np.abs(array).min() if vmin is None else vmin
                vmax = np.abs(array).max() if vmax is None else vmax
                add_domain_coloring_cbar(ax, vmin, vmax)
            else:
                plt.colorbar(im, ax=ax)

        return ax, im


class LineProfiles(AbstractMeasurement):

    def __init__(self,
                 array: np.ndarray,
                 start: Tuple[float, float] = None,
                 end: Tuple[float, float] = None,
                 sampling: float = None,
                 extra_axes_metadata: List[AxisMetadata] = None,
                 metadata: dict = None):
        from abtem.waves.scan import LineScan

        if start is None:
            start = (0., 0.)

        if end is None:
            end = (start[0] + len(array) * sampling, start[1])

        self._linescan = LineScan(start=start, end=end, sampling=sampling, gpts=array.shape[-1])
        super().__init__(array=array, extra_axes_metadata=extra_axes_metadata, metadata=metadata)

    @property
    def start(self) -> Tuple[float, float]:
        return self._linescan.start

    @property
    def end(self) -> Tuple[float, float]:
        return self._linescan.end

    @property
    def extent(self) -> float:
        return self._linescan.extent[0]

    @property
    def sampling(self) -> float:
        return self._linescan.sampling[0]

    @property
    def base_axes_metadata(self) -> List[AxisMetadata]:
        return [RealSpaceAxis(label='x', sampling=self.sampling, units='Å')]

    def interpolate(self,
                    sampling: float = None,
                    gpts: int = None,
                    padding: str = 'wrap',
                    kind: str = None) -> 'LineProfiles':

        if kind is None:
            kind = 'quadratic'

        # endpoint = self.calibrations[-1].endpoint
        # sampling = self.calibrations[-1].sampling
        # offset = self.calibrations[-1].offset

        endpoint = False

        extent = self.sampling * (self.array.shape[-1] - endpoint)

        grid = Grid(extent=extent, gpts=gpts, sampling=sampling, endpoint=endpoint, dimensions=1)

        array = np.pad(self.array, ((5,) * 2,), mode=padding)
        pad = 5 / self.array.shape[-1]
        # points = self._linescan.get_positions(lazy=False)

        points = np.linspace(-pad, 1 + pad, len(array), endpoint=endpoint)

        # new_linescan = self._linescan.copy()
        # new_linescan.sampling = sampling

        # new_points = new_linescan.get_positions(lazy=False)
        new_points = np.linspace(0, 1, grid.gpts[0], endpoint=endpoint)

        interpolator = interp1d(points, array, kind=kind)

        # x = np.linspace(offset, offset + extent, new_grid.gpts[0], endpoint=endpoint)

        array = interpolator(new_points)
        # calibrations = [calibration.copy() for calibration in self.calibrations]
        # calibrations[-1].sampling = new_grid.sampling[0]

        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        d['sampling'] = grid.sampling[0]
        return self.__class__(**d)

    def tile(self, reps):
        d = self._copy_as_dict(copy_array=False)
        d['end'] = np.array(self.start) + (np.array(self.end) - np.array(self.start)) * reps
        d['array'] = np.tile(self.array, reps)
        return self.__class__(**d)
        # new_copy._array

    def to_hyperspy(self):
        from hyperspy._signals.signal1d import Signal1D
        return Signal1D(self.array, axes=_to_hyperspy_axes_metadata(self.axes_metadata, self.shape)).as_lazy()

    def _copy_as_dict(self, copy_array=True) -> dict:
        d = {'start': self.start,
             'end': self.end,
             'sampling': self.sampling,
             'extra_axes_metadata': self.extra_axes_metadata,
             'metadata': self.metadata}

        if copy_array:
            d['array'] = self.array.copy()

        return d

    def show(self, ax=None, title='', label=None):
        if ax is None:
            ax = plt.subplot()

        array = copy_to_device(self.array.reshape((-1, self.array.shape[-1])).T, np)

        ax.plot(array, label=label)
        ax.set_title(title)

        return ax


class RadialFourierSpaceLineProfiles(LineProfiles):

    def __init__(self, array, sampling, extra_axes_metadata=None, metadata=None):
        super().__init__(array=array, start=(0., 0.), end=(0., array.shape[-1] * sampling), sampling=sampling,
                         extra_axes_metadata=extra_axes_metadata, metadata=metadata)

    def show(self, ax=None, title='', **kwargs):
        if ax is None:
            ax = plt.subplot()

        x = np.linspace(0., len(self.array) * self.sampling * 1000, len(self.array))

        p = ax.plot(x, self.array, **kwargs)
        ax.set_xlabel('Scattering angle [mrad]')
        ax.set_title(title)
        return ax, p


def bilinear_nodes_and_weight(old_shape, new_shape, old_angular_sampling, new_angular_sampling, xp):
    nodes = []
    weights = []

    old_sampling = (1 / old_angular_sampling[0] / old_shape[0],
                    1 / old_angular_sampling[1] / old_shape[1])

    new_sampling = (1 / new_angular_sampling[0] / new_shape[0],
                    1 / new_angular_sampling[1] / new_shape[1])

    for n, m, r, d in zip(old_shape, new_shape, old_sampling, new_sampling):
        k = xp.fft.fftshift(xp.fft.fftfreq(n, r).astype(xp.float32))
        k_new = xp.fft.fftshift(xp.fft.fftfreq(m, d).astype(xp.float32))
        distances = k_new[None] - k[:, None]
        distances[distances < 0.] = np.inf
        w = distances.min(0) / (k[1] - k[0])
        w[w == np.inf] = 0.
        nodes.append(distances.argmin(0))
        weights.append(w)

    v, u = nodes
    vw, uw = weights
    v, u, vw, uw = xp.broadcast_arrays(v[:, None], u[None, :], vw[:, None], uw[None, :])
    return v, u, vw, uw


def integrate_gradient_2d(gradient, sampling):
    gx, gy = gradient.real, gradient.imag
    (nx, ny) = gx.shape[-2:]
    ikx = np.fft.fftfreq(nx, d=sampling[0])
    iky = np.fft.fftfreq(ny, d=sampling[1])
    grid_ikx, grid_iky = np.meshgrid(ikx, iky, indexing='ij')
    k = grid_ikx ** 2 + grid_iky ** 2
    k[k == 0] = 1e-12
    That = (np.fft.fft2(gx) * grid_ikx + np.fft.fft2(gy) * grid_iky) / (2j * np.pi * k)
    T = np.real(np.fft.ifft2(That))
    T -= np.min(T)
    return T


class DiffractionPatterns(AbstractMeasurement, HasAcceleratorMixin):

    def __init__(self,
                 array,
                 sampling: Tuple[float, float],
                 energy: float = None,
                 fftshift: bool = False,
                 extra_axes_metadata: List[AxisMetadata] = None,
                 metadata: dict = None):

        self._fftshift = fftshift
        self._sampling = float(sampling[0]), float(sampling[1])
        self._accelerator = Accelerator(energy=energy)
        super().__init__(array=array, extra_axes_metadata=extra_axes_metadata, metadata=metadata)

    def poisson_noise(self, dose: float, samples=1):
        pixel_area = np.prod(self.scan_sampling)
        d = self._copy_as_dict(copy_array=False)

        def add_poisson_noise(array, _):
            array = array * dose * pixel_area
            xp = get_array_module(array)
            return xp.random.poisson(array).astype(xp.float32)

        arrays = []
        for i in range(samples):
            arrays.append(self.array.map_blocks(add_poisson_noise, _=i))

        arrays = da.stack(arrays)

        d['array'] = arrays
        d['extra_axes_metadata'] = [OrdinalAxis()] + d['extra_axes_metadata']

        return self.__class__(**d)

    @property
    def base_axes_metadata(self):
        return [FourierSpaceAxis(sampling=self.angular_sampling[0], label='x', units='mrad'),
                FourierSpaceAxis(sampling=self.angular_sampling[1], label='y', units='mrad')]

    def to_hyperspy(self):
        from hyperspy._signals.signal2d import Signal2D
        return Signal2D(self.array, axes=_to_hyperspy_axes_metadata(self.axes_metadata, self.shape)).as_lazy()

    def _copy_as_dict(self, copy_array: bool = True) -> dict:
        d = {'sampling': self.sampling,
             'energy': self.energy,
             'extra_axes_metadata': copy.deepcopy(self.extra_axes_metadata),
             'metadata': copy.deepcopy(self.metadata),
             'fftshift': self.fftshift}
        if copy_array:
            d['array'] = self.array.copy()
        return d

    def __getitem__(self, items):
        return self._get_measurements(items)

    @property
    def fftshift(self):
        return self._fftshift

    @property
    def sampling(self) -> Tuple[float, float]:
        return self._sampling

    @property
    def angular_sampling(self) -> Tuple[float, float]:
        self.accelerator.check_is_defined()
        return self.sampling[0] * self.wavelength * 1e3, self.sampling[1] * self.wavelength * 1e3

    @property
    def max_angles(self):
        return self.shape[-2] // 2 * self.angular_sampling[0], self.shape[-1] // 2 * self.angular_sampling[1]

    @property
    def equivalent_real_space_extent(self):
        return 1 / self._sampling[0], 1 / self._sampling[1]

    @property
    def equivalent_real_space_sampling(self):
        return 1 / self._sampling[0] / self.base_shape[0], 1 / self._sampling[1] / self.base_shape[1]

    @property
    def angular_extent(self):
        limits = []
        for i in (-2, -1):
            if self.shape[i] % 2:
                limits += [(-(self.shape[i] - 1) // 2 * self.angular_sampling[i],
                            (self.shape[i] - 1) // 2 * self.angular_sampling[i])]
            else:
                limits += [(-self.shape[i] // 2 * self.angular_sampling[i],
                            (self.shape[i] // 2 - 1) * self.angular_sampling[i])]
        return limits

    def interpolate(self, new_sampling):
        xp = get_array_module(self.array)

        if new_sampling == 'uniform':
            scale_factor = (self.angular_sampling[0] / max(self.angular_sampling),
                            self.angular_sampling[1] / max(self.angular_sampling))

            new_gpts = (int(np.ceil(self.base_shape[0] * scale_factor[0])),
                        int(np.ceil(self.base_shape[1] * scale_factor[1])))

            if np.abs(new_gpts[0] - new_gpts[1]) <= 2:
                new_gpts = (min(new_gpts),) * 2

            new_angular_sampling = (self.angular_sampling[0] / scale_factor[0],
                                    self.angular_sampling[1] / scale_factor[1])

        else:
            raise RuntimeError('')

        v, u, vw, uw = bilinear_nodes_and_weight(self.array.shape[-2:],
                                                 new_gpts,
                                                 self.angular_sampling,
                                                 new_angular_sampling,
                                                 xp)

        return interpolate_bilinear(self.array, v, u, vw, uw)

    def _check_integration_limits(self, inner, outer):
        if inner >= outer:
            raise RuntimeError(f'inner detection ({inner} mrad) angle exceeds outer detection angle'
                               f'({outer} mrad)')

        if (outer > self.max_angles[0]) or (outer > self.max_angles[1]):
            raise RuntimeError(
                f'outer integration limit exceeds the maximum simulated angle ({outer} mrad > '
                f'{min(self.max_angles)} mrad)')

        integration_range = outer - inner
        if integration_range < min(self.angular_sampling):
            raise RuntimeError(
                f'integration range ({integration_range} mrad) smaller than angular sampling of simulation'
                f' ({min(self.angular_sampling)} mrad)')

    def gaussian_filter(self, sigma: Union[float, Tuple[float, float]], boundary: str = 'periodic'):
        xp = get_array_module(self.array)
        ndimage = get_ndimage_module(self._array)

        gaussian_filter = ndimage.gaussian_filter

        if np.isscalar(sigma):
            sigma = (sigma,) * 2

        sampling = [self.axes_metadata[axis]['sampling'] for axis in self.scan_axes]

        sigma = self.num_ensemble_axes * (0,) + tuple(s / d for s, d in zip(sigma, sampling)) + (0,) * 2

        array = self.array.map_overlap(gaussian_filter,
                                       sigma=sigma,
                                       boundary=boundary,
                                       depth=self.num_ensemble_axes * (0,) + (int(np.ceil(4.0 * max(sigma))),) * 2 +
                                             (0,) * 2,
                                       meta=xp.array((), dtype=xp.float32))

        return self.__class__(array, angular_sampling=self.angular_sampling,
                              extra_axes_metadata=self.extra_axes_metadata,
                              metadata=self.metadata, fftshift=self.fftshift)

    def polar_binning(self, nbins_radial, nbins_azimuthal, inner, outer, rotation=0.):

        self._check_integration_limits(inner, outer)
        xp = get_array_module(self.array)

        def radial_binning(array, nbins_radial, nbins_azimuthal):
            xp = get_array_module(array)

            indices = polar_detector_bins(gpts=array.shape[-2:],
                                          sampling=self.angular_sampling,
                                          inner=inner,
                                          outer=outer,
                                          nbins_radial=nbins_radial,
                                          nbins_azimuthal=nbins_azimuthal,
                                          fftshift=self.fftshift,
                                          rotation=rotation,
                                          return_indices=True)

            separators = xp.concatenate((xp.array([0]), xp.cumsum(xp.array([len(i) for i in indices]))))

            new_shape = array.shape[:-2] + (nbins_radial, nbins_azimuthal)

            array = array.reshape((-1, array.shape[-2] * array.shape[-1],))[..., np.concatenate(indices)]

            result = xp.zeros((array.shape[0], len(indices),), dtype=xp.float32)

            if xp is cp:
                sum_run_length_encoded_cuda(array, result, separators)

            else:
                sum_run_length_encoded(array, result, separators)

            return result.reshape(new_shape)

        if self.is_lazy:
            array = self.array.map_blocks(radial_binning, nbins_radial=nbins_radial,
                                          nbins_azimuthal=nbins_azimuthal,
                                          drop_axis=(len(self.shape) - 2, len(self.shape) - 1),
                                          chunks=self.array.chunks[:-2] + ((nbins_radial,), (nbins_azimuthal,),),
                                          new_axis=(len(self.shape) - 2, len(self.shape) - 1,),
                                          meta=xp.array((), dtype=xp.float32))
        else:
            array = radial_binning(self.array, nbins_radial=nbins_radial, nbins_azimuthal=nbins_azimuthal)

        radial_sampling = (outer - inner) / nbins_radial
        azimuthal_sampling = 2 * np.pi / nbins_azimuthal

        return PolarMeasurements(array,
                                 radial_sampling=radial_sampling,
                                 azimuthal_sampling=azimuthal_sampling,
                                 radial_offset=inner,
                                 azimuthal_offset=rotation,
                                 extra_axes_metadata=self.extra_axes_metadata,
                                 metadata=self.metadata)

    def radial_binning(self, step_size=1., inner=0., outer=None):
        if outer is None:
            outer = min(self.max_angles)

        nbins_radial = int((outer - inner) / step_size)
        return self.polar_binning(nbins_radial, 1, inner, outer)

    def _create_image_or_lineprofiles(self, array):
        if self.num_scan_axes not in (1, 2):
            raise RuntimeError()

        extra_axes_metadata = [element for i, element in enumerate(self.extra_axes_metadata) if not i in self.scan_axes]

        if len(self.scan_axes) == 1:
            start = self.scan_axes_metadata[0].start
            end = self.scan_axes_metadata[0].end

            return LineProfiles(array, sampling=self.axes_metadata[self.scan_axes[0]].sampling, start=start, end=end,
                                extra_axes_metadata=extra_axes_metadata, metadata=self.metadata)
        else:
            sampling = self.axes_metadata[self.scan_axes[0]].sampling, self.axes_metadata[self.scan_axes[1]].sampling
            return Images(array, sampling=sampling, extra_axes_metadata=extra_axes_metadata, metadata=self.metadata)

    def integrate_radial(self, inner, outer):
        self._check_integration_limits(inner, outer)

        xp = get_array_module(self.array)

        def integrate_fourier_space(array, sampling):

            bins = polar_detector_bins(gpts=array.shape[-2:],
                                       sampling=sampling,
                                       inner=inner,
                                       outer=outer,
                                       nbins_radial=1,
                                       nbins_azimuthal=1,
                                       fftshift=self.fftshift)

            xp = get_array_module(array)
            bins = xp.asarray(bins, dtype=xp.float32)

            return xp.sum(array * (bins == 0), axis=(-2, -1))

        if self.is_lazy:
            integrated_intensity = self.array.map_blocks(integrate_fourier_space,
                                                         sampling=self.angular_sampling,
                                                         drop_axis=(len(self.shape) - 2, len(self.shape) - 1),
                                                         meta=xp.array((), dtype=xp.float32))
        else:
            integrated_intensity = integrate_fourier_space(self.array, sampling=self.angular_sampling)

        return self._create_image_or_lineprofiles(integrated_intensity)

    def integrated_center_of_mass(self) -> Images:
        array = self.center_of_mass().array
        array = array.rechunk(array.chunks[:-2] + ((array.shape[-2],), (array.shape[-1],)))
        array = array.map_blocks(integrate_gradient_2d, sampling=self.scan_sampling, dtype=np.float32)
        return Images(array=array, sampling=self.scan_sampling, extra_axes_metadata=self.extra_axes_metadata[:-2],
                      metadata=self.metadata)

    def center_of_mass(self) -> Images:

        def com(array):
            x, y = self.angular_coordinates()
            com_x = (array * x[:, None]).sum(axis=(-2, -1))
            com_y = (array * y[None]).sum(axis=(-2, -1))
            com = com_x + 1.j * com_y
            return com

        array = self.array.map_blocks(com, drop_axis=self.base_axes, dtype=np.complex64)
        return self._create_image_or_lineprofiles(array)

    def angular_coordinates(self) -> Tuple[np.ndarray, np.ndarray]:
        xp = get_array_module(self.array)
        alpha_x = xp.linspace(self.angular_extent[0][0], self.angular_extent[0][1], self.shape[-2], dtype=xp.float32)
        alpha_y = xp.linspace(self.angular_extent[1][0], self.angular_extent[1][1], self.shape[-1], dtype=xp.float32)
        return alpha_x, alpha_y

    def bandlimit(self, radius):
        if radius is None:
            radius = max(self.angular_sampling) * 1.1

        def block_direct(array):
            alpha_x, alpha_y = self.angular_coordinates()
            alpha = alpha_x[:, None] ** 2 + alpha_y[None] ** 2
            block = alpha < radius ** 2
            return array * block

        xp = get_array_module(self.array)

        if self.is_lazy:
            array = da.from_delayed(dask.delayed(block_direct)(self.array), shape=self.shape,
                                    meta=xp.array((), dtype=xp.float32))
        else:
            array = block_direct(self.array)

        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        return self.__class__(**d)

    def block_direct(self, radius: float = None) -> 'DiffractionPatterns':

        if radius is None:
            radius = max(self.angular_sampling) * 1.1

        def block_direct(array):
            alpha_x, alpha_y = self.angular_coordinates()
            alpha = alpha_x[:, None] ** 2 + alpha_y[None] ** 2
            block = alpha > radius ** 2
            return array * block

        xp = get_array_module(self.array)

        if self.is_lazy:
            array = da.from_delayed(dask.delayed(block_direct)(self.array), shape=self.shape,
                                    meta=xp.array((), dtype=xp.float32))
        else:
            array = block_direct(self.array)

        d = self._copy_as_dict(copy_array=False)
        d['array'] = array
        return self.__class__(**d)

    def show(self, ax=None, cbar=False, power=1., figsize=None, max_angle=None, **kwargs):
        if ax is None:
            fig, ax = plt.subplots(figsize=figsize)

        slic = (0,) * self.num_extra_axes
        extent = self.angular_extent[0] + self.angular_extent[1]

        array = asnumpy(self._array)[slic].T ** power

        im = ax.imshow(array, extent=extent, origin='lower', **kwargs)
        ax.set_xlabel('Scattering angle x [mrad]')
        ax.set_ylabel('Scattering angle y [mrad]')

        if max_angle:
            ax.set_xlim([-max_angle, max_angle])
            ax.set_ylim([-max_angle, max_angle])

        if cbar:
            plt.colorbar(im, ax=ax)

        return ax, im


class PolarMeasurements(AbstractMeasurement):

    def __init__(self,
                 array: np.ndarray,
                 radial_sampling: float,
                 azimuthal_sampling: float,
                 radial_offset: float = 0.,
                 azimuthal_offset: float = 0.,
                 extra_axes_metadata: List[AxisMetadata] = None,
                 metadata: dict = None):

        self._radial_sampling = radial_sampling
        self._azimuthal_sampling = azimuthal_sampling
        self._radial_offset = radial_offset
        self._azimuthal_offset = azimuthal_offset

        super().__init__(array=array, extra_axes_metadata=extra_axes_metadata, metadata=metadata)

    @property
    def base_axes_metadata(self) -> List[AxisMetadata]:
        return [LinearAxis(label='Radial scattering angle', sampling=self.radial_sampling, units='mrad'),
                LinearAxis(label='Azimuthal scattering angle', sampling=self.azimuthal_sampling, units='rad')]

    def to_hyperspy(self):
        raise NotImplementedError

    @property
    def radial_offset(self) -> float:
        return self._radial_offset

    @property
    def outer_angle(self) -> float:
        return self._radial_offset + self.radial_sampling * self.shape[-2]

    @property
    def radial_sampling(self) -> float:
        return self._radial_sampling

    @property
    def azimuthal_sampling(self) -> float:
        return self._azimuthal_sampling

    @property
    def azimuthal_offset(self) -> float:
        return self._azimuthal_offset

    def _check_radial_angle(self, angle):
        if angle < self.radial_offset or angle > self.outer_angle:
            raise RuntimeError()

    def integrate_radial(self, inner, outer) -> Union[Images, LineProfiles]:
        return self.integrate(radial_limits=(inner, outer))

    def integrate(self,
                  radial_limits: Tuple[float, float] = None,
                  azimutal_limits: Tuple[float, float] = None,
                  detector_regions: Sequence[int] = None) -> Union[Images, LineProfiles]:

        sampling = self.scan_sampling

        if detector_regions is not None:
            array = self.array.reshape(self.shape[:-2] + (-1,))[..., list(detector_regions)].sum(axis=-1)
            return Images(array=array, sampling=sampling, extra_axes_metadata=self.extra_axes_metadata[:-2],
                          metadata=self.metadata)

        if radial_limits is None:
            radial_slice = slice(None)
        else:
            inner_index = int((radial_limits[0] - self.radial_offset) / self.radial_sampling)
            outer_index = int((radial_limits[1] - self.radial_offset) / self.radial_sampling)
            radial_slice = slice(inner_index, outer_index)

        if azimutal_limits is None:
            azimuthal_slice = slice(None)
        else:
            left_index = int(azimutal_limits[0] / self.radial_sampling)
            right_index = int(azimutal_limits[1] / self.radial_sampling)
            azimuthal_slice = slice(left_index, right_index)

        array = self.array[..., radial_slice, azimuthal_slice].sum(axis=(-2, -1))

        return Images(array=array, sampling=sampling, extra_axes_metadata=self.extra_axes_metadata[:-2],
                      metadata=self.metadata)

    def _copy_as_dict(self, copy_array: bool = True) -> dict:
        d = {'radial_offset': self.radial_offset,
             'radial_sampling': self.radial_sampling,
             'azimuthal_offset': self.azimuthal_offset,
             'azimuthal_sampling': self.azimuthal_sampling,
             'extra_axes_metadata': copy.deepcopy(self.extra_axes_metadata),
             'metadata': copy.deepcopy(self.metadata)}
        if copy_array:
            d['array'] = self.array.copy()

        return d

    def show(self, ax=None, min_azimuthal_division=np.pi / 20, **kwargs):
        array = self.array[(0,) * (len(self.shape) - 2)]

        repeat = int(self.azimuthal_sampling / min_azimuthal_division)
        r = np.pi / (4 * repeat) + self.azimuthal_offset
        azimuthal_grid = np.linspace(r, 2 * np.pi + r, self.shape[-1] * repeat, endpoint=False)

        d = (self.outer_angle - self.radial_offset) / 2 / self.shape[-2]
        radial_grid = np.linspace(self.radial_offset + d, self.outer_angle - d, self.shape[-2])

        z = np.repeat(array, repeat, axis=-1)
        r, th = np.meshgrid(radial_grid, azimuthal_grid)

        if ax is None:
            ax = plt.subplot(projection="polar")

        im = ax.pcolormesh(th, r, z.T, shading='auto', **kwargs)
        ax.set_rlim([0, self.outer_angle * 1.1])

        ax.grid()
        return ax, im
