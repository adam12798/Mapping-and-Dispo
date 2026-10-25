"""List — and, with --apply, delete — Ventana's duplicate reps.

A candidate is a Ventana rep row that copies a Team Sunshine rep: the same
phone number (digits only), or, when either of the two has no number, the
same name. Migration 0041 made such a copy of every Team Sunshine rep.

Each candidate is listed with every row, in any organization, that points at
it — through every relation a delete would walk (Django's own list, hidden
relations included). A candidate is kept when anything points at it, or when
it is active (Ventana's working reps are 0041 copies too); every other
candidate is marked "delete".

--apply re-reads each row marked "delete" under a lock, and deletes only
inactive Ventana rows that nothing points at — that re-check is what keeps a
delete from clearing (SET_NULL) or cascading into any other row. Inside the
same transaction it refuses, changing nothing, if a row changed since it was
listed, if the delete reached any row but the reps themselves (a cascade), or
if Team Sunshine's rep count would change.

A dry run by default; output goes to stdout only (no file, no log; a refusal
is printed there too, and the command exits 1):

    python3 manage.py cleanup_duplicate_reps            # list only
    python3 manage.py cleanup_duplicate_reps --apply    # delete the rows marked "delete"
"""
import re
import sys

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models.deletion import get_candidate_relations_to_delete

from maps.models import Organization, Rep

VENTANA = 'ventana'
SUNSHINE = 'team-sunshine'


def digits(phone):
    d = re.sub(r'\D', '', phone or '')
    return d[1:] if len(d) == 11 and d.startswith('1') else d


def masked(phone):
    d = digits(phone)
    return f'…{d[-4:]}' if len(d) >= 4 else '(no number)'


class Refused(Exception):
    """Raised inside the transaction, so it rolls back; printed to stdout."""


def references(rep):
    """Every relation that points at this rep, with its count: [(label, n)] —
    the relations a delete walks (hidden ones too). Unscoped (each model's base
    manager is all_objects), so a row in any organization counts."""
    found = []
    for rel in get_candidate_relations_to_delete(Rep._meta):
        n = rel.related_model._base_manager.filter(**{rel.field.name: rep.pk}).count()
        if n:
            found.append((f'{rel.related_model._meta.label}.{rel.field.name}', n))
    return found


def sunshine_count(org):
    return Rep.all_objects.filter(organization=org).count()


def candidates(ventana, sunshine):
    """[(rep, twin_id, basis, refs, keep)] for each Ventana rep that copies a
    Team Sunshine rep; keep is None (delete), 'referenced' or 'active'."""
    twins = list(Rep.all_objects.filter(organization=sunshine).values('id', 'name', 'phone_number'))
    by_phone = {}
    by_name = {}
    for t in twins:
        if digits(t['phone_number']):
            by_phone.setdefault(digits(t['phone_number']), t['id'])
        if t['name'].strip():
            by_name.setdefault(t['name'].strip().casefold(), t)
    out = []
    for rep in Rep.all_objects.filter(organization=ventana).order_by('id'):
        d = digits(rep.phone_number)
        twin, basis = (by_phone.get(d), 'phone') if d else (None, None)
        if twin is None:
            t = by_name.get(rep.name.strip().casefold())
            if t and (not d or not digits(t['phone_number'])):
                twin, basis = t['id'], 'name'
        if twin is None:
            continue
        refs = references(rep)
        keep = 'referenced' if refs else ('active' if rep.is_active else None)
        out.append((rep, twin, basis, refs, keep))
    return out


class Command(BaseCommand):
    help = "List Ventana's rep rows that copy a Team Sunshine rep; --apply deletes the inactive ones nothing points at."

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='delete the rows marked "delete" (default: list only)')

    def handle(self, *args, **opts):
        try:
            self._run(opts['apply'])
        except Refused as e:
            self.stdout.write(f'REFUSED: {e}')
            sys.exit(1)

    def _run(self, apply):
        orgs = {o.slug: o for o in Organization.objects.filter(slug__in=[VENTANA, SUNSHINE])}
        if set(orgs) != {VENTANA, SUNSHINE}:
            raise Refused(f'both organizations must exist: {VENTANA}, {SUNSHINE}')
        ventana, sunshine = orgs[VENTANA], orgs[SUNSHINE]
        rows = candidates(ventana, sunshine)
        self.stdout.write(f"Ventana rep rows that copy a Team Sunshine rep — {'APPLY' if apply else 'dry run'}")
        for rep, twin, basis, refs, keep in rows:
            ref_text = ', '.join(f'{label} x{n}' for label, n in refs) or 'none'
            verdict = 'delete' if keep is None else f'keep ({keep})'
            self.stdout.write(
                f'  rep {rep.id} "{rep.name}" {masked(rep.phone_number)} active={"yes" if rep.is_active else "no"}'
                f' | copies Team Sunshine rep {twin} (same {basis}) | references: {ref_text} | {verdict}')
        doomed = [rep for rep, _t, _b, _r, keep in rows if keep is None]
        kept = len(rows) - len(doomed)
        self.stdout.write(f'Candidates: {len(rows)}; to delete: {len(doomed)}; kept: {kept}.')

        if not apply:
            self.stdout.write('Dry run: nothing deleted. Run again with --apply to delete the rows marked "delete".')
            return
        if not doomed:
            self.stdout.write('Nothing to delete.')
            return

        with transaction.atomic():
            before = sunshine_count(sunshine)
            locked = list(Rep.all_objects.select_for_update()
                          .filter(id__in=[r.id for r in doomed], organization=ventana, is_active=False))
            if len(locked) != len(doomed):
                raise Refused('a row changed during the run (organization or active); nothing deleted')
            for rep in locked:
                if references(rep):
                    raise Refused(f'rep {rep.id} gained a reference during the run; nothing deleted')
            _total, by_model = Rep.all_objects.filter(id__in=[r.id for r in locked]).delete()
            if by_model != {Rep._meta.label: len(locked)}:
                raise Refused(f'the delete would cascade into other rows ({by_model}); nothing deleted')
            after = sunshine_count(sunshine)
            if after != before:
                raise Refused(f"Team Sunshine's rep count would change ({before} → {after}); nothing deleted")
        self.stdout.write(f'Deleted {len(locked)} Ventana rep row(s): {", ".join(str(r.id) for r in locked)}. '
                          f"Team Sunshine's rep count is unchanged ({after}).")
