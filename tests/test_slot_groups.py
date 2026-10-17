from copy import deepcopy
from datetime import datetime

import pytest

from padelbot.slot_groups import group_slots, interval_label


def slot(product=1, court=101, name='Veld 1', *, start='2030-06-05T08:30:00+02:00',
         end='2030-06-05T10:00:00+02:00', price=8):
    return dict(product=product, court=court, name=name, start=start, end=end, price=price)


def test_one_full_interval_keeps_naturally_ordered_original_court_choices():
    rows = [slot(10, 110, 'Veld 10'), slot(2, 102, 'Veld 2'), slot(), slot()]
    original = deepcopy(rows)
    groups = group_slots(rows)
    assert len(groups) == 1
    assert groups[0]['id'] == '2030-06-05T06:30:00Z/2030-06-05T08:00:00Z'
    assert interval_label(groups[0]) == '08:30–10:00'
    assert [court['name'] for court in groups[0]['courts']] == ['Veld 1', 'Veld 2', 'Veld 10']
    assert groups[0]['courts'][1] is rows[1]
    assert rows == original
    assert group_slots(reversed(rows)) == groups


def test_equal_start_with_different_end_overlap_adjacent_and_other_date_stay_separate():
    rows = [slot(end='2030-06-05T09:30:00+02:00'), slot(),
        slot(start='2030-06-05T09:00:00+02:00', end='2030-06-05T10:30:00+02:00'),
        slot(start='2030-06-05T10:00:00+02:00', end='2030-06-05T11:30:00+02:00'),
        slot(start='2030-06-06T08:30:00+02:00', end='2030-06-06T10:00:00+02:00')]
    groups = group_slots(reversed(rows))
    assert len(groups) == 5 and len({group['id'] for group in groups}) == 5
    assert [group['courts'][0] for group in groups] == rows


def test_timezone_spellings_group_by_instant_and_retain_original_timestamps():
    original = slot()
    utc = slot(2, 102, 'Veld 2', start='2030-06-05T06:30:00.000Z', end='2030-06-05T08:00:00Z')
    groups = group_slots([utc, original])
    assert len(groups) == 1
    assert groups[0]['courts'] == [original, utc]
    assert groups[0]['start'] == original['start'] and groups[0]['end'] == original['end']
    assert groups[0]['courts'][1]['start'] == '2030-06-05T06:30:00.000Z'


def test_autumn_dst_repeated_clock_is_two_distinct_intervals_and_labels():
    first = slot(start='2030-10-27T02:00:00+02:00', end='2030-10-27T02:30:00+02:00')
    second = slot(start='2030-10-27T02:00:00+01:00', end='2030-10-27T02:30:00+01:00')
    groups = group_slots([second, first])
    assert len(groups) == 2
    assert groups[0]['id'] != groups[1]['id']
    assert interval_label(groups[0]) == '02:00 CEST–02:30 CEST'
    assert interval_label(groups[1]) == '02:00 CET–02:30 CET'


def test_duplicate_identity_is_only_removed_within_its_exact_interval():
    rows = [slot(), slot(), slot(court=102), slot(product=2),
            slot(end='2030-06-05T10:30:00+02:00')]
    groups = group_slots(rows)
    assert [len(group['courts']) for group in groups] == [3, 1]
    assert group_slots([]) == []


@pytest.mark.parametrize('start,end', [
    ('2030-06-05T08:30:00', '2030-06-05T10:00:00+02:00'),
    ('2030-06-05T08:30:00+02:00', '2030-06-05T08:30:00+02:00'),
    ('2030-06-05T08:30:00+02:00', '2030-06-05T08:00:00+02:00'),
])
def test_invalid_or_unzoned_intervals_fail_instead_of_fabricating_choices(start, end):
    with pytest.raises(ValueError):
        group_slots([slot(start=start, end=end)])


def test_worker_exposes_shared_groups_and_preserves_the_flat_canonical_payload(monkeypatch):
    from padelbot import booking, client, web_worker
    from padelbot.booking import Slot

    rows = [slot(10, 110, 'Veld 10'), slot(2, 102, 'Veld 2'), slot(), slot(),
            slot(end='2030-06-05T11:00:00+02:00')]
    domain = [Slot(row['product'], row['court'], row['start'], row['end'], row['price'], {}) for row in rows]
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def me(self): return {'id': 42}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat('2030-06-05T06:00:00+02:00').astimezone(tz)
    monkeypatch.setattr(web_worker, 'datetime', Clock)
    monkeypatch.setattr(web_worker, 'initialise_key', lambda: None)
    monkeypatch.setattr(client, 'BackboneClient', Client)
    monkeypatch.setattr(booking, 'find_slots_window', lambda *args, **kwargs: domain)
    monkeypatch.setattr(booking, 'get_product_names', lambda *args: {1: 'Veld 1', 2: 'Veld 2', 10: 'Veld 10'})
    result = web_worker.execute('slots', {'start': 0, 'end': 6})
    assert result['slots'] == rows
    assert result['slot_groups'] == group_slots(result['slots'])
    assert [len(group['courts']) for group in result['slot_groups']] == [3, 1]
