#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calculate cycle-averaged tidal flow field using approach of Mariotti (2018)
"""

import numpy as np
from landlab import Component, RasterModelGrid, HexModelGrid
from landlab.utils import make_core_node_matrix_var_coef
from landlab.grid.mappers import map_mean_of_link_nodes_to_link

_FOUR_THIRDS = 4.0 / 3.0
_M2_PERIOD = (12.0 + (25.2 / 60.0)) * 3600.0  # M2 tidal period, in seconds


class TidalFlowCalculator(Component):
    """Calculate average flow over a tidal cycle."""

    _name = "TidalFlowCalculator"

    _unit_agnostic = False

    _info = {
        "flood_tide_flow__velocity": {
            "dtype": float,
            "intent": "out",
            "optional": False,
            "units": "m/s",
            "mapping": "link",
            "doc": "Horizontal flow velocity along links during flood tide",
        },
        "ebb_tide_flow__velocity": {
            "dtype": float,
            "intent": "out",
            "optional": False,
            "units": "m/s",
            "mapping": "link",
            "doc": "Horizontal flow velocity along links during ebb tide",
        },
        "topographic__elevation": {
            "dtype": float,
            "intent": "in",
            "optional": False,
            "units": "m",
            "mapping": "node",
            "doc": "Bathymetric/topographic elevation",
        },
        "mean_water__depth": {
            "dtype": float,
            "intent": "out",
            "optional": False,
            "units": "m",
            "mapping": "node",
            "doc": "Tidal mean water depth",
        },
    }

    def __init__(
        self,
        grid,
        tidal_range=1.0,
        tidal_period=_M2_PERIOD,
        roughness=0.01,
        scale_velocity=1.0,
        mean_sea_level=0.0,
        min_water_depth=0.01,
    ):
        """Initialize TidalFlowCalculator."""

        # Handle grid type
        if isinstance(grid, RasterModelGrid):
            self._grid_multiplier = 1.0
        elif grid is HexModelGrid:
            self._grid_multiplier = 1.5
        else:
            raise TypeError("Grid must be raster or hex.")

        # Call base class methods to check existence of input fields,
        # create output fields, etc.
        super().__init__(grid)
        self.initialize_output_fields()

        # Get references to various fields, for convenience
        self._elev = self.grid.at_node["topographic__elevation"]
        self._water_depth = self.grid.at_node["mean_water__depth"]
        self._flood_tide_vel = self.grid.at_link["flood_tide_flow__velocity"]
        self._ebb_tide_vel = self.grid.at_link["ebb_tide_flow__velocity"]

        # Handle inputs
        # TODO: make properties
        self.tidal_range = tidal_range
        self._tidal_half_range = tidal_range / 2.0
        self.tidal_period = tidal_period
        self._tidal_half_period = tidal_period / 2.0
        self._scale_velocity = scale_velocity
        self.roughness = roughness  # TODO: replace with a field
        self.mean_sea_level = mean_sea_level
        self._min_depth = min_water_depth

        # Make other data structures
        self._water_depth_at_links = np.zeros(grid.number_of_links)
        self._diffusion_coef_at_links = np.zeros(grid.number_of_links)

    def calc_tidal_inundation_rate(self):
        """Calculate and store the rate of inundation/draining at each node, averaged over a tidal half-cycle.

        Examples
        --------
        >>> grid = RasterModelGrid((3, 5))
        >>> z = grid.add_zeros('topographic__elevation', at='node')
        >>> z[5:10] = [10.0, 0.25, 0.0, -0.25, -10.0]
        >>> period=4.0e4  # tidal period in s, for convenient calculation
        >>> tfc = TidalFlowCalculator(grid, tidal_period=period)
        >>> rate = tfc.calc_tidal_inundation_rate()
        >>> 0.5 * rate[5:10] * period  # depth in m
        array([ 0.  ,  0.25,  0.5 ,  0.75,  1.  ])
        >>> rate[5:10]  # rate in m/s
        array([  0.00000000e+00,   1.25000000e-05,   2.50000000e-05,
                  3.75000000e-05,   5.00000000e-05])

        Notes
        -----
        This calculates $I$ in Mariotti (2018) using his equation (1).
        """
        return (
            self._tidal_half_range
            - np.maximum(
                -self._tidal_half_range, np.minimum(self._elev, self._tidal_half_range)
            )
        ) / self._tidal_half_period

    def run_one_step(self):
        """Calculate the tidal flow field and water-surface elevation.

        TODO: use an "out" for matrix fn when available; use sparse option
        when available.
        """
        print('hi')
        # Tidal mean water depth  and water surface elevation at nodes
        self._water_depth[:] = self.mean_sea_level - self._elev
        self._water_depth[self._water_depth <= 0.0] = self._min_depth
        mean_water_surf_elev = self._elev + self._water_depth

        # Map water depth to links
        map_mean_of_link_nodes_to_link(
            self.grid, self._water_depth, out=self._water_depth_at_links
        )

        # Calculate velocity and diffusion coefficients on links
        velocity_coef = self._water_depth_at_links ** _FOUR_THIRDS / (
            (self.roughness ** 2) * self._scale_velocity
        )
        self._diffusion_coef_at_links[:] = self._water_depth_at_links * velocity_coef

        # Calculate inundation / drainage rate at nodes
        tidal_inundation_rate = self.calc_tidal_inundation_rate()

        # Set up right-hand-side (RHS) vector for both ebb and flood tides (only
        # difference is in the sign)
        cores = self.grid.core_nodes
        dx = self.grid.dx
        base_rhs = self._grid_multiplier * tidal_inundation_rate[cores] * dx * dx

        # For flood tide, set up matrix and add boundary info to RHS vector
        mat, rhs = make_core_node_matrix_var_coef(
            self.grid, mean_water_surf_elev, self._diffusion_coef_at_links
        )
        rhs[:, 0] += base_rhs

        # Solve for flood tide water-surface elevation
        tidal_wse = np.zeros(self.grid.number_of_nodes)
        tidal_wse[self.grid.core_nodes] = np.dot(np.linalg.inv(mat), rhs).flatten()
        print(tidal_wse)
        # Calculate flood-tide water-surface gradient at links
        tidal_wse_grad = self.grid.calc_grad_at_link(tidal_wse)

        # Calculate flow velocity field at links for flood tide
        self._flood_tide_vel[self.grid.active_links] = (
            -velocity_coef[self.grid.active_links]
            * tidal_wse_grad[self.grid.active_links]
        )
        print(self._flood_tide_vel)
        # For ebb tide, set up matrix and add boundary info to RHS vector
        mat, rhs = make_core_node_matrix_var_coef(
            self.grid, mean_water_surf_elev, self._diffusion_coef_at_links
        )
        rhs[:, 0] -= base_rhs

        # Solve for ebb tide water-surface elevation
        tidal_wse[cores] = np.dot(np.linalg.inv(mat), rhs).flatten()

        # Calculate ebb-tide water-surface gradient at links
        self.grid.calc_grad_at_link(tidal_wse, out=tidal_wse_grad)

        # Calculate flow velocity field at links for ebb tide
        self._ebb_tide_vel[self.grid.active_links] = (
            -velocity_coef[self.grid.active_links]
            * tidal_wse_grad[self.grid.active_links]
        )

# TODO:
# - add test for hex grid
# - allow roughness to be a field
# - make properties
# - use sparse matrix
# - add "out" to matrix fns
# - make demo notebook

