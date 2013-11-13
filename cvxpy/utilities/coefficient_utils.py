"""
Copyright 2013 Steven Diamond

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

from .. import interface as intf
from .. import settings as s

# Operations on a dict of Variable to coefficient for an affine expression.

def format_coeffs(coefficients):
    """Reduces variable coefficients to scalars if possible.

    Args:
        coefficients: A dict of Variable object to ndarray.
    """
    for _, blocks in coefficients.items():
        for i, block in enumerate(blocks):
            if intf.is_scalar(block):
                blocks[i] = intf.scalar_value(block)
    return coefficients

def index(coeffs, key):
    """Indexes/slices into the coefficients of each variable.

    Args:
        key: A (slice, slice) tuple.

    Returns:
        A dict with the indexed/sliced coefficients.
    """
    new_coeffs = {}
    for var_id, blocks in coeffs.items():
        new_blocks = []
        # Indexes into the rows of the coefficients.
        for block in blocks[key[1]]:
            block_key = (key[0], slice(None, None, None))
            block_val = intf.index(block, block_key)
            new_blocks.append(block_val)
        new_coeffs[var_id] = new_blocks

    return format_coeffs(new_coeffs)

    # # Multiplies by a ones matrix to promote scalar coefficients.
    # # Returns an updated coefficient dict.
    # @staticmethod
    # def promote(coeffs, shape):
    #     rows,cols = shape.size
    #     ones = intf.DEFAULT_SPARSE_INTERFACE.ones(rows, 1)
    #     new_coeffs = {}
    #     for key,blocks in coeffs.items():
    #         new_coeffs[key] = [ones*blocks[0] for i in range(cols)]
    #     return new_coeffs

def add(lh_coeffs, rh_coeffs):
    """Determines the coefficients of two expressions added together.

    Args:
        lh_coeffs: The coefficents of the left-hand expression.
        rh_coeffs: The coefficents of the right-hand expression.

    Returns:
        The coefficients of the sum.
    """
    # Merge the dicts, summing common variables.
    new_coeffs = lh_coeffs.copy()
    for var_id, blocks in rh_coeffs.items():
        if var_id in new_coeffs:
            new_coeffs[var_id] = new_coeffs[var_id] + blocks
        else:
            new_coeffs[var_id] = blocks
    return new_coeffs

def sub(lh_coeffs, rh_coeffs):
    """Determines the coefficients of the difference of two expressions.

    Args:
        lh_coeffs: The coefficents of the left-hand expression.
        rh_coeffs: The coefficents of the right-hand expression.

    Returns:
        The coefficients of the difference.
    """
    return add(lh_coeffs, neg(rh_coeffs))

def _merge_cols(blocks):
    """Utility method to merge column blocks into a single matrix.

    Args:
        blocks: An ndarray of coefficients for columns.

    Returns:
        A cvxopt spmatrix or a scalar.
    """
    rows = intf.size(blocks[0])[0]
    cols = len(blocks)
    # Check for scalars.
    if (rows, cols) == (1, 1):
        return blocks[0]
    interface = intf.DEFAULT_SPARSE_INTERFACE
    result = interface.zeros(rows, cols)
    for i in xrange(cols):
        result[:, i] = blocks[i]
    return result

def mul(lh_coeffs, rh_coeffs):
    """Determines the coefficients of two expressions multiplied together.

    Args:
        lh_coeffs: The coefficents of the left-hand expression.
        other: The coefficents of the right-hand expression.

    Returns:
        The coefficients of the product.
    """
    # Distributes multiplications by left hand constant
    # across right hand terms.
    lh_blocks = lh_coeffs[s.CONSTANT]
    constant_term = _merge_cols(lh_blocks)
    new_coeffs = {}
    for var_id, blocks in rh_coeffs.items():
        # For scalars distribute across constant blocks.
        if len(blocks) == 1 and intf.size(blocks[0])[0] == 1:
            new_coeffs[var_id] = lh_blocks * blocks[0]
        # For matrices distribute constant across coefficient blocks.
        else:
            new_coeffs[var_id] = constant_term * blocks
    return format_coeffs(new_coeffs)

def neg(coeffs):
    """Negates the coefficients of every variable.
    """
    new_coeffs = {}
    for var_id, blocks in coeffs.items():
        new_coeffs[var_id] = -blocks
    return new_coeffs
