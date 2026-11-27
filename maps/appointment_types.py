"""Appointment types per organization (Organization.appointment_types).

Each organization keeps its own list of the Lead.appointment_type values it
offers, as data editable without code (Django admin, or the shell):

    [{"value": "solar", "label": "Solar", "any_rep": false}, ...]

- value: what Lead.appointment_type stores (1-10 lowercase letters, digits
  or "_"; the column is 10 characters).
- label: what the CRM, Daily, Dashboard and provider pages show.
- any_rep: true = any active rep of the organization may be assigned it,
  whatever the rep's specialty (maps/assignment.py is_compatible). False or
  missing = today's specialty rule.

Every organization starts with default_appointment_types(): exactly the three
types Sutton has always had (Lead.APPOINTMENT_TYPE_CHOICES), none open to
any rep, so an organization with the default list sees and assigns exactly
as before. The list is not a filter on writes: the v1 API and the CRM store
any value sent, as they always have.
"""
import re

from django.core.exceptions import ValidationError

VALUE_RE = re.compile(r'^[a-z0-9_]{1,10}$')
ENTRY_KEYS = frozenset({'value', 'label', 'any_rep'})
EXAMPLE = '{"value": "windows", "label": "Windows", "any_rep": true}'
# The three types Sutton has always had. Their colours (pins, charts) and the
# tag-derived type (compute_appointment_type) belong to them alone.
BASE_VALUES = frozenset({'solar', 'hvac', 'both'})
# Chart colours for an organization's other types, in list order (cycled).
# None is a base type's colour, the "unknown" pink or the chart fallback.
EXTRA_COLORS = ('#3d5a80', '#8e44ad', '#e67e22', '#16a085', '#7f8c8d', '#2c3e50', '#d35400')
# Map pin for a type the organization lists beyond the base three. A value
# outside its list keeps the pink "type or format missing" pin.
EXTRA_PIN_COLOR = '#7f8c8d'


def default_appointment_types():
    """A fresh copy of the default list (a JSONField default must be a
    callable). Same values, labels and order as Lead.APPOINTMENT_TYPE_CHOICES."""
    return [
        {'value': 'solar', 'label': 'Solar', 'any_rep': False},
        {'value': 'hvac', 'label': 'HVAC', 'any_rep': False},
        {'value': 'both', 'label': 'Both', 'any_rep': False},
    ]


def validate_appointment_types(value):
    """Runs in the Django admin and full_clean(): a non-empty list of
    {"value", "label", "any_rep"} objects with unique values."""
    if not isinstance(value, list) or not value:
        raise ValidationError(f'Enter a non-empty list of objects like {EXAMPLE}.')
    seen = set()
    for i, entry in enumerate(value, 1):
        if not isinstance(entry, dict):
            raise ValidationError(f'Item {i} must be an object like {EXAMPLE}.')
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise ValidationError(f'Item {i} has unknown keys: {", ".join(sorted(unknown))}.')
        v = entry.get('value')
        if not isinstance(v, str) or not VALUE_RE.match(v):
            raise ValidationError(
                f'Item {i}: "value" must be 1-10 lowercase letters, digits or _ (e.g. "windows").')
        if v in seen:
            raise ValidationError(f'Item {i}: value "{v}" is listed twice.')
        seen.add(v)
        label = entry.get('label')
        if not isinstance(label, str) or not label.strip():
            raise ValidationError(f'Item {i}: "label" must be non-empty text.')
        if 'any_rep' in entry and not isinstance(entry['any_rep'], bool):
            raise ValidationError(f'Item {i}: "any_rep" must be true or false.')


def normalize(raw):
    """The entries to use, in the configured order, each as
    {'value', 'label', 'any_rep'}. Tolerant of a list saved without
    validation: unusable entries are skipped, a missing label shows the value,
    any_rep is on only when it is literally true, and a list with nothing
    usable reads as the default."""
    entries, seen = [], set()
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            v = entry.get('value')
            if not isinstance(v, str) or not v or v in seen:
                continue
            seen.add(v)
            label = entry.get('label')
            entries.append({
                'value': v,
                'label': label if isinstance(label, str) and label.strip() else v,
                'any_rep': entry.get('any_rep') is True,
            })
    return entries or default_appointment_types()


def for_org(org_id):
    """Entries of organization `org_id`; the default list when there is no
    organization (or no such row)."""
    raw = None
    if org_id is not None:
        from .models import Organization
        raw = Organization.objects.filter(id=org_id).values_list('appointment_types', flat=True).first()
    return normalize(raw)


def labels(entries):
    """{value: label} for display lookups."""
    return {e['value']: e['label'] for e in entries}


def labels_for_org(org_id):
    return labels(for_org(org_id))


def any_rep_values(entries):
    """Values any active rep of the organization may take."""
    return frozenset(e['value'] for e in entries if e['any_rep'])


def any_rep_values_for_org(org_id):
    return any_rep_values(for_org(org_id))


def extra_values(entries):
    """Listed values beyond the base three, in list order."""
    return [e['value'] for e in entries if e['value'] not in BASE_VALUES]


def is_extra(entries, value):
    """True when `value` is one of the organization's own types beyond the
    base three. The tag pills never re-derive such a type (they only know
    solar/hvac/both). Always False for the default list."""
    return value in extra_values(entries)


def extra_colors(entries):
    """{value: chart colour} for the types beyond the base three. Empty for
    the default list."""
    return {v: EXTRA_COLORS[i % len(EXTRA_COLORS)] for i, v in enumerate(extra_values(entries))}


def sorted_by_label(entries):
    """Entries ordered by label, ignoring case (the filter dropdowns' order:
    Both, HVAC, Solar for the default list)."""
    return sorted(entries, key=lambda e: e['label'].lower())
