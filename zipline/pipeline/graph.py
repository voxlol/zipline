"""
Dependency-Graph representation of Pipeline API terms.
"""
from networkx import (
    DiGraph,
    topological_sort,
)
from six import iteritems, itervalues
from zipline.utils.memoize import lazyval
from zipline.pipeline.visualize import display_graph

from .term import LoadableTerm


class CyclicDependency(Exception):
    pass


class TermGraph(DiGraph):
    """
    An abstract representation of Pipeline Term dependencies.

    This class does not keep any additional metadata about any term relations
    other than dependency ordering.  As such it is only useful in contexts
    where you care exclusively about order properties (for example, when
    drawing visualizations of execution order).

    Parameters
    ----------
    terms : dict
        A dict mapping names to final output terms.

    Attributes
    ----------
    outputs

    Methods
    -------
    ordered()
        Return a topologically-sorted iterator over the terms in self.

    See Also
    --------
    ExecutionPlan
    """
    def __init__(self, terms):
        super(TermGraph, self).__init__()

        self._frozen = False
        parents = set()
        for term in itervalues(terms):
            self._add_to_graph(term, parents)
            # No parents should be left between top-level terms.
            assert not parents

        self._outputs = terms
        self._ordered = topological_sort(self)

        # Mark that no more terms should be added to the graph.
        self._frozen = True

    def _add_to_graph(self, term, parents):
        """
        Add a term and all its children to ``graph``.

        ``parents`` is the set of all the parents of ``term` that we've added
        so far. It is only used to detect dependency cycles.
        """
        if self._frozen:
            raise ValueError(
                "Can't mutate %s after construction." % type(self).__name__
            )

        # If we've seen this node already as a parent of the current traversal,
        # it means we have an unsatisifiable dependency.  This should only be
        # possible if the term's inputs are mutated after construction.
        if term in parents:
            raise CyclicDependency(term)

        parents.add(term)

        self.add_node(term)

        for dependency in term.dependencies:
            self._add_to_graph(dependency, parents)
            self.add_edge(dependency, term)

        parents.remove(term)

    @property
    def outputs(self):
        """
        Dict mapping names to designated output terms.
        """
        return self._outputs

    def ordered(self):
        """
        Return a topologically-sorted iterator over the terms in `self`.
        """
        return iter(self._ordered)

    @lazyval
    def loadable_terms(self):
        return tuple(term for term in self if isinstance(term, LoadableTerm))

    @lazyval
    def jpeg(self):
        return display_graph(self, 'jpeg')

    @lazyval
    def png(self):
        return display_graph(self, 'png')

    @lazyval
    def svg(self):
        return display_graph(self, 'svg')

    def _repr_png_(self):
        return self.png.data

    def initial_refcounts(self, initial_workspace):
        """
        Calculate initial refcounts for execution of this graph.

        Each node starts with a refcount equal to its outdegree, and output
        nodes get one extra reference to ensure that they're still in the graph
        at the end of execution.
        """
        refcounts = self.out_degree()
        for t in self.outputs.values():
            refcounts[t] += 1

        for t in initial_workspace:
            self.decref_dependencies(t, refcounts)

        return refcounts

    def decref_dependencies(self, term, refcounts):
        """
        Decrement in-edges for ``term`` after computation.

        Parameters
        ----------
        term : zipline.pipeline.Term
            The term whose parents should be decref'ed.
        refcounts : dict[Term -> int]
            Dictionary of refcounts.

        Return
        ------
        garbage : set[Term]
            Terms whose refcounts hit zero after decrefing.
        """
        garbage = set()
        # Edges are tuple of (from, to).
        for parent, _ in self.in_edges([term]):
            refcounts[parent] -= 1
            # No one else depends on this term. Remove it from the
            # workspace to conserve memory.
            if refcounts[parent] == 0:
                garbage.add(parent)
        return garbage


class ExecutionPlan(TermGraph):
    """
    Graph represention of Pipeline Term dependencies that includes metadata
    about extra rows required to perform computations.

    Each node in the graph has an `extra_rows` attribute, indicating how many,
    if any, extra rows we should compute for the node.  Extra rows are most
    often needed when a term is an input to a rolling window computation.  For
    example, if we compute a 30 day moving average of price from day X to day
    Y, we need to load price data for the range from day (X - 29) to day Y.

    Parameters
    ----------
    terms : dict
        A dict mapping names to final output terms.
    all_dates : pd.DatetimeIndex
        An index of all known trading days for which ``terms`` will be
        computed.
    start_date : pd.Timestamp
        The first date for which output is requested for ``terms``.
    end_date : pd.Timestamp
        The last date for which output is requested for ``terms``.

    Attributes
    ----------
    outputs
    offset
    extra_rows

    Methods
    -------
    ordered()
        Return a topologically-sorted iterator over the terms in self.
    """
    def __init__(self,
                 terms,
                 all_dates,
                 start_date,
                 end_date,
                 min_extra_rows=0):
        super(ExecutionPlan, self).__init__(terms)

        for term in terms.values():
            self.set_extra_rows(
                term,
                all_dates,
                start_date,
                end_date,
                min_extra_rows=min_extra_rows,
            )

    def set_extra_rows(self,
                       term,
                       all_dates,
                       start_date,
                       end_date,
                       min_extra_rows):
        """
        Compute ``extra_rows`` for transitive dependencies of ``root_terms``
        """
        # A term can require that additional extra rows beyond the minimum be
        # computed.  This is most often used with downsampled terms, which need
        # to ensure that the first date is a computation date.
        extra_rows_for_term = term.compute_extra_rows(
            all_dates,
            start_date,
            end_date,
            min_extra_rows,
        )
        if extra_rows_for_term < min_extra_rows:
            raise ValueError(
                "term %s requested fewer rows than the minimum of %d" % (
                    term, min_extra_rows,
                )
            )

        self._ensure_extra_rows(term, extra_rows_for_term)

        for dependency, additional_extra_rows in term.dependencies.items():
            self.set_extra_rows(
                dependency,
                all_dates,
                start_date,
                end_date,
                min_extra_rows=extra_rows_for_term + additional_extra_rows,
            )

    @lazyval
    def offset(self):
        """
        For all pairs (term, input) such that `input` is an input to `term`,
        compute a mapping::

            (term, input) -> offset(term, input)

        where ``offset(term, input)`` is the number of rows that ``term``
        should truncate off the raw array produced for ``input`` before using
        it. We compute this value as follows::

            offset(term, input) = (extra_rows_computed(input)
                                   - extra_rows_computed(term)
                                   - requested_extra_rows(term, input))
        Examples
        --------

        Case 1
        ~~~~~~

        Factor A needs 5 extra rows of USEquityPricing.close, and Factor B
        needs 3 extra rows of the same.  Factor A also requires 5 extra rows of
        USEquityPricing.high, which no other Factor uses.  We don't require any
        extra rows of Factor A or Factor B

        We load 5 extra rows of both `price` and `high` to ensure we can
        service Factor A, and the following offsets get computed::

            offset[Factor A, USEquityPricing.close] == (5 - 0) - 5 == 0
            offset[Factor A, USEquityPricing.high]  == (5 - 0) - 5 == 0
            offset[Factor B, USEquityPricing.close] == (5 - 0) - 3 == 2
            offset[Factor B, USEquityPricing.high] raises KeyError.

        Case 2
        ~~~~~~

        Factor A needs 5 extra rows of USEquityPricing.close, and Factor B
        needs 3 extra rows of Factor A, and Factor B needs 2 extra rows of
        USEquityPricing.close.

        We load 8 extra rows of USEquityPricing.close (enough to load 5 extra
        rows of Factor A), and the following offsets get computed::

            offset[Factor A, USEquityPricing.close] == (8 - 3) - 5 == 0
            offset[Factor B, USEquityPricing.close] == (8 - 0) - 2 == 6
            offset[Factor B, Factor A]              == (3 - 0) - 3 == 0

        Notes
        -----
        `offset(term, input) >= 0` for all valid pairs, since `input` must be
        an input to `term` if the pair appears in the mapping.

        This value is useful because we load enough rows of each input to serve
        all possible dependencies.  However, for any given dependency, we only
        want to compute using the actual number of required extra rows for that
        dependency.  We can do so by truncating off the first `offset` rows of
        the loaded data for `input`.

        See Also
        --------
        zipline.pipeline.graph.TermGraph.offset
        zipline.pipeline.engine.SimplePipelineEngine._inputs_for_term
        zipline.pipeline.engine.SimplePipelineEngine._mask_and_dates_for_term
        """
        extra = self.extra_rows
        return {
            # Another way of thinking about this is:
            # How much bigger is the array for ``dep`` compared to ``term``?
            # How much of that difference did I ask for.
            (term, dep): (extra[dep] - extra[term]) - requested_extra_rows
            for term in self
            for dep, requested_extra_rows in term.dependencies.items()
        }

    @lazyval
    def extra_rows(self):
        """
        A dict mapping `term` -> `# of extra rows to load/compute of `term`.

        Notes
        ----
        This value depends on the other terms in the graph that require `term`
        **as an input**.  This is not to be confused with `term.dependencies`,
        which describes how many additional rows of `term`'s inputs we need to
        load, and which is determined entirely by `Term` itself.

        Example
        -------
        Our graph contains the following terms:

            A = SimpleMovingAverage([USEquityPricing.high], window_length=5)
            B = SimpleMovingAverage([USEquityPricing.high], window_length=10)
            C = SimpleMovingAverage([USEquityPricing.low], window_length=8)

        To compute N rows of A, we need N + 4 extra rows of `high`.
        To compute N rows of B, we need N + 9 extra rows of `high`.
        To compute N rows of C, we need N + 7 extra rows of `low`.

        We store the following extra_row requirements:

        self.extra_rows[high] = 9  # Ensures that we can service B.
        self.extra_rows[low] = 7

        See Also
        --------
        zipline.pipeline.graph.TermGraph.offset
        zipline.pipeline.term.Term.dependencies
        """
        return {
            term: attrs['extra_rows']
            for term, attrs in iteritems(self.node)
        }

    def _ensure_extra_rows(self, term, N):
        """
        Ensure that we're going to compute at least N extra rows of `term`.
        """
        attrs = self.node[term]
        attrs['extra_rows'] = max(N, attrs.get('extra_rows', 0))
