import duotypes as t
from database import api_tx
from typing import Tuple, Optional
from service.search.sql import *

def _quiz_search_results(searcher_person_id: int):
    params = dict(
        searcher_person_id=searcher_person_id,
    )

    with api_tx('READ COMMITTED') as tx:
        return tx.execute(Q_QUIZ_SEARCH, params).fetchall()


def _uncached_search_results(searcher_person_id: int, no: Tuple[int, int]):
    with api_tx('READ COMMITTED') as tx:
        params_1 = dict(
            searcher_person_id=searcher_person_id,
        )

        tx.execute(Q_UNCACHED_SEARCH_1, params_1)

        n, o = no
        params_2 = dict(
            searcher_person_id=searcher_person_id,
            n=n,
            o=o,
        )

        return tx.execute(Q_UNCACHED_SEARCH_2, params_2).fetchall()


def _cached_search_results(searcher_person_id: int, no: Tuple[int, int]):
    n, o = no

    params = dict(
        searcher_person_id=searcher_person_id,
        n=n,
        o=o
    )

    with api_tx('READ COMMITTED') as tx:
        return tx.execute(Q_CACHED_SEARCH, params).fetchall()

def get_search_type(n: Optional[str], o: Optional[str]):
    n_: Optional[int] = n if n is None else int(n)
    o_: Optional[int] = o if o is None else int(o)

    if n_ is not None and not n_ >= 0:
        raise ValueError('n must be >= 0')
    if o_ is not None and not o_ >= 0:
        raise ValueError('o must be >= 0')

    no = None if (n_ is None or o_ is None) else (n_, o_)

    if no is None:
        return 'quiz-search', no
    elif no[1] == 0:
        return 'uncached-search', no
    else:
        return 'cached-search', no


def get_search(s: t.SessionInfo, n: Optional[str], o: Optional[str]):
    search_type, no = get_search_type(n, o)

    if no is not None and no[0] > 10:
        return 'n must be less than or equal to 10', 400
    elif search_type == 'quiz-search':
        return _quiz_search_results(searcher_person_id=s.person_id)
    elif search_type == 'uncached-search':
        return _uncached_search_results(searcher_person_id=s.person_id, no=no)
    elif search_type == 'cached-search':
        return _cached_search_results(searcher_person_id=s.person_id, no=no)
    else:
        raise Exception('Unexpected quiz type')
