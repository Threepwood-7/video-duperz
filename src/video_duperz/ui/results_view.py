"""Public results view entry point composed from focused mixin modules."""

from __future__ import annotations

from . import results_view_shared as _shared
from .results_view_actions import ResultsViewActionMixin

SORT_GROUP_COUNT_ASC = _shared.SORT_GROUP_COUNT_ASC
SORT_GROUP_COUNT_DESC = _shared.SORT_GROUP_COUNT_DESC
SORT_GROUP_SIZE_ASC = _shared.SORT_GROUP_SIZE_ASC
SORT_GROUP_SIZE_DESC = _shared.SORT_GROUP_SIZE_DESC
SORT_GROUP_SPREAD_ASC = _shared.SORT_GROUP_SPREAD_ASC
SORT_GROUP_SPREAD_DESC = _shared.SORT_GROUP_SPREAD_DESC
SORT_ROW_SIZE_ASC = _shared.SORT_ROW_SIZE_ASC
SORT_ROW_SIZE_DESC = _shared.SORT_ROW_SIZE_DESC


class ResultsView(ResultsViewActionMixin):
    """Results table widget that renders duplicate groups and row actions."""
