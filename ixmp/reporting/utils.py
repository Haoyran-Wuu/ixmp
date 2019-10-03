import collections
from collections.abc import Collection
from copy import deepcopy
from functools import partial, reduce
import logging
from operator import mul

import pandas as pd
from pandas.core.generic import NDFrame
import pint
import xarray as xr

from .key import Key


log = logging.getLogger(__name__)

ureg = pint.UnitRegistry()


# Replacements to apply to quantity units before parsing by pint
REPLACE_UNITS = {
    '%': 'percent',
}


def clean_units(input_string):
    """Tolerate messy strings for units.

    Handles two specific cases found in |MESSAGEix| test cases:

    - Dimensions enclosed in '[]' have these characters stripped.
    - The '%' symbol cannot be supported by pint, because it is a Python
      operator; it is translated to 'percent'.

    """
    input_string = input_string.strip('[]')
    for old, new in REPLACE_UNITS.items():
        input_string = input_string.replace(old, new)
    return input_string


def collect_units(*args):
    """Return an list of '_unit' attributes for *args*."""
    for arg in args:
        if '_unit' in arg.attrs:
            # Convert units if necessary
            if isinstance(arg.attrs['_unit'], str):
                arg.attrs['_unit'] = ureg.parse_units(arg.attrs['_unit'])
        else:
            log.debug('assuming {} is unitless'.format(arg))
            arg.attrs['_unit'] = ureg.parse_units('')

    return [arg.attrs['_unit'] for arg in args]


# Mapping from raw -> preferred dimension names
rename_dims = {}


def _find_dims(data):
    """Return the list of dimensions for *data*."""
    if isinstance(data, pd.DataFrame):
        # List of the dimensions
        dims = data.columns.tolist()
    else:
        dims = list(data)

    # Remove columns containing values or units; dimensions are the remainder
    for col in 'value', 'lvl', 'mrg', 'unit':
        try:
            dims.remove(col)
        except ValueError:
            continue

    # Rename dimensions
    return [rename_dims.get(d, d) for d in dims]


def keys_for_quantity(ix_type, name, scenario):
    """Iterate over keys for *name* in *scenario*."""
    # Retrieve names of the indices of the low-level/Java object, *without*
    # loading the associated data
    # NB this is used instead of .getIdxSets, since the same set may index more
    #    than one dimension of the same variable.
    dims = _find_dims(scenario._item(ix_type, name, load=False)
                      .getIdxNames().toArray())

    # Column for retrieving data
    column = 'value' if ix_type == 'par' else 'lvl'

    # A computation to retrieve the data
    key = Key(name, dims)
    yield (key, (partial(data_for_quantity, ix_type, name, column),
                 'scenario', 'filters'))

    # Add the marginal values at full resolution, but no aggregates
    if ix_type == 'equ':
        yield (Key('{}-margin'.format(name), dims),
               (partial(data_for_quantity, ix_type, name, 'mrg'),
                'scenario', 'filters'))

    # Partial sums
    yield from key.iter_sums()


def _parse_units(units_series):
    """Return a :class:`pint.Unit` for a :class:`pd.Series` of strings."""
    unit = pd.unique(units_series)

    if len(unit) > 1:
        # py3.5 compat: could use an f-string here
        raise ValueError('mixed units {!r}'.format(list(unit)))

    # Helper method to return an intelligible exception
    def invalid(unit):
        chars = ''.join(c for c in '-?$' if c in unit)
        return ValueError(("unit {!r} cannot be parsed; contains invalid "
                           "character(s) {!r}").format(unit, chars))

    # Parse units
    try:
        unit = clean_units(unit[0])
        unit = ureg.parse_units(unit)
    except IndexError:
        # Quantity has no unit
        unit = ureg.parse_units('')
    except pint.UndefinedUnitError:
        # Unit(s) do not exist; define them in the UnitRegistry

        # Split possible compound units
        for u in unit.split('/'):
            if u in dir(ureg):
                # Unit already defined
                continue

            # py3.5 compat: could use f-strings here
            definition = '{0} = [{0}]'.format(u)
            log.info('Add unit definition: {}'.format(definition))

            # This line will fail silently for units like 'G$'
            ureg.define(definition)

        # Try to parse again
        try:
            unit = ureg.parse_units(unit)
        except pint.UndefinedUnitError:
            # Handle the silent failure of define(), above
            raise invalid(unit) from None
    except AttributeError:
        # Unit contains a character like '-' that throws off pint
        # NB this 'except' clause must be *after* UndefinedUnitError, since
        #    that is a subclass of AttributeError.
        raise invalid(unit)

    return unit


class AttrSeries(pd.Series):
    """:class:`pandas.Series` subclass imitating :class:`xarray.DataArray`.

    Future versions of :mod:`ixmp.reporting` will use :class:`xarray.DataArray`
    as :class:`Quantity`; however, because :mod:`xarray` currently lacks sparse
    matrix support, ixmp quantities may be too large for memory.

    The AttrSeries class provides similar methods and behaviour to
    :class:`xarray.DataArray`, such as an `attrs` dictionary for metadata, so
    that :mod:`ixmp.reporting.computations` methods can use xarray-like syntax.
    """

    # normal properties
    _metadata = ('attrs', )

    def __init__(self, *args, **kwargs):
        if 'attrs' in kwargs:
            # Use provided attrs
            attrs = kwargs.pop('attrs')
        elif hasattr(args[0], 'attrs'):
            # Use attrs from an xarray object
            attrs = args[0].attrs.copy()

            # pre-convert an pd.Series to preserve names and labels
            args = list(args)
            args[0] = args[0].to_series()
        else:
            # default empty
            attrs = collections.OrderedDict()

        super().__init__(*args, **kwargs)

        self.attrs = attrs

    def assign_attrs(self, d):
        self.attrs.update(d)
        return self

    def assign_coords(self, **kwargs):
        return pd.concat([self], keys=kwargs.values(), names=kwargs.keys())

    @property
    def coords(self):
        """Read-only."""
        return dict(zip(self.index.names, self.index.levels))

    @property
    def dims(self):
        return tuple(self.index.names)

    def sel(self, indexers=None, drop=False, **indexers_kwargs):
        indexers = indexers or {}
        indexers.update(indexers_kwargs)
        if len(indexers) == 1:
            level, key = list(indexers.items())[0]
            if not isinstance(key, Collection) and not drop:
                # When using .loc[] to select 1 label on 1 level, pandas drops
                # the level. Use .xs() to avoid this behaviour unless drop=True
                return AttrSeries(self.xs(key, level=level, drop_level=False))

        idx = tuple(indexers.get(l, slice(None)) for l in self.index.names)
        return AttrSeries(self.loc[idx])

    def sum(self, *args, **kwargs):
        try:
            dim = kwargs.pop('dim')
            if isinstance(self.index, pd.MultiIndex):
                if len(dim) == len(self.index.names):
                    # assume dimensions = full multi index, do simple sum
                    obj = self
                    kwargs = {}
                else:
                    # pivot and sum across columns
                    obj = self.unstack(dim)
                    kwargs['axis'] = 1
            else:
                if dim != [self.index.name]:
                    raise ValueError(dim, self.index.name, self)
                obj = super()
                kwargs['level'] = dim
        except KeyError:
            obj = super()
        return AttrSeries(obj.sum(*args, **kwargs))

    def squeeze(self, *args, **kwargs):
        kwargs.pop('drop')
        return super().squeeze(*args, **kwargs) if len(self) > 1 else self

    def as_xarray(self):
        return xr.DataArray.from_series(self)

    def transpose(self, *dims):
        return self.reorder_levels(dims)

    def to_dataframe(self):
        return self.to_frame()

    def to_series(self):
        return self

    @property
    def _constructor(self):
        return AttrSeries

    def __finalize__(self, other, method=None, **kwargs):
        """Propagate metadata from other to self.

        This is identical to the version in pandas, except deepcopy() is added
        so that the 'attrs' OrderedDict is not double-referenced.
        """
        if isinstance(other, NDFrame):
            for name in self._metadata:
                object.__setattr__(self, name,
                                   deepcopy(getattr(other, name, None)))
        return self


def data_for_quantity(ix_type, name, column, scenario, filters=None):
    """Retrieve data from *scenario*.

    Parameters
    ----------
    ix_type : 'equ' or 'par' or 'var'
        Type of the ixmp object.
    name : str
        Name of the ixmp object.
    column : 'mrg' or 'lvl' or 'value'
        Data to retrieve. 'mrg' and 'lvl' are valid only for ``ix_type='equ'``,
        and 'level' otherwise.
    scenario : ixmp.Scenario
        Scenario containing data to be retrieved.
    filters : dict, optional
        Mapping from dimensions to iterables of allowed values along each
        dimension.

    Returns
    -------
    :class:`Quantity`
        Data for *name*.
    """
    log.debug('Retrieving data for {}'.format(name))
    # Retrieve quantity data
    data = scenario._element(ix_type, name, filters)

    # ixmp/GAMS scalar is not returned as pd.DataFrame
    if isinstance(data, dict):
        data = pd.DataFrame.from_records([data])

    # List of the dimensions
    dims = _find_dims(data)

    # Remove the unit from the DataFrame
    try:
        attrs = {'_unit': _parse_units(data.pop('unit'))}
    except KeyError:
        # 'equ' are returned without units
        attrs = {}
    except ValueError as e:
        if 'mixed units' in e.args[0]:
            # Discard mixed units
            log.warn('{} discarded for {!r}'.format(e.args[0], name))
            attrs = {'_unit': ureg.parse_units('')}
        else:
            # Raise all other ValueErrors
            raise

    # Set index if 1 or more dimensions
    if len(dims):
        # First rename, then set index
        data.rename(columns=rename_dims, inplace=True)
        data.set_index(dims, inplace=True)

    # Check sparseness
    try:
        shape = list(map(len, data.index.levels))
    except AttributeError:
        shape = [data.index.size]
    size = reduce(mul, shape)
    filled = 100 * len(data) / size if size else 'NA'
    need_to_chunk = size > 1e7 and filled < 1
    info = (name, shape, filled, size, need_to_chunk)
    log.debug(' '.join(map(str, info)))

    # Convert to a Dataset, assign attrbutes and name
    # ds = xr.Dataset.from_dataframe(data)[column]
    # or to a new "Attribute Series"
    ds = AttrSeries(data[column])

    ds = ds \
        .assign_attrs(attrs) \
        .rename(name + ('-margin' if column == 'mrg' else ''))

    try:
        # Remove length-1 dimensions for scalars
        ds = ds.squeeze('index', drop=True)
    except KeyError:
        pass

    return ds


# Quantity = xr.DataArray
Quantity = AttrSeries


def concat(*args, **kwargs):
    if Quantity is AttrSeries:
        kwargs.pop('dim')
        return pd.concat(*args, **kwargs)
    elif Quantity is xr.DataArray:
        return xr.concat(*args, **kwargs)
