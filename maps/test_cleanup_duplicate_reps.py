"""Guardrail 13: the duplicate-rep cleanup — dry run by default, every
candidate listed with its references, a referenced (or active) row kept,
only Ventana rows deleted, and a refusal if Team Sunshine's rep count would
change. Fictional 555 numbers only."""
from datetime import date
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from .models import Lead, Rep, TimeOffRequest, UserProfile, VoiceCallLog
from .tests import TenancyBase


class CleanupBase(TenancyBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        mk = Rep.all_objects.create
        # Team Sunshine's reps, and Ventana's 0041-style copies of them
        cls.s_pat = mk(name='Pat Copy', phone_number='6175550011', home_address='x', organization=cls.sunshine)
        cls.v_pat = mk(name='Pat Copy', phone_number='+1 (617) 555-0011', home_address='x', is_active=False,
                       organization=cls.ventana)  # inactive, nothing points at it: delete
        cls.s_lee = mk(name='Lee Copy', phone_number='6175550012', home_address='x', organization=cls.sunshine)
        cls.v_lee = mk(name='Lee Copy', phone_number='6175550012', home_address='x', is_active=True,
                       organization=cls.ventana)  # active: keep
        cls.s_kim = mk(name='Kim Copy', phone_number='6175550013', home_address='x', organization=cls.sunshine)
        cls.v_kim = mk(name='Kim Copy', phone_number='6175550013', home_address='x', is_active=False,
                       organization=cls.ventana)  # a lead points at it: keep
        Lead.all_objects.create(address='1 Q St', homeowner_name='Q', organization=cls.ventana, rep=cls.v_kim)
        cls.s_ash = mk(name='Ash Copy', phone_number='', home_address='x', organization=cls.sunshine)
        cls.v_ash = mk(name='ash copy ', phone_number='', home_address='x', is_active=False,
                       organization=cls.ventana)  # no numbers, same name: delete
        # same name, different numbers: not a copy
        mk(name='Pat Copy', phone_number='6175550014', home_address='x', is_active=False, organization=cls.ventana)

    def run_cmd(self, *args):
        out = StringIO()
        call_command('cleanup_duplicate_reps', *args, stdout=out)
        return out.getvalue()

    def ids(self, org):
        return set(Rep.all_objects.filter(organization=org).values_list('id', flat=True))


class DryRunTests(CleanupBase):
    def test_lists_every_candidate_and_deletes_nothing(self):
        before_v, before_s = self.ids(self.ventana), self.ids(self.sunshine)
        out = self.run_cmd()
        self.assertIn('dry run', out)
        self.assertIn(f'rep {self.v_pat.id} "Pat Copy" …0011 active=no | copies Team Sunshine rep {self.s_pat.id} '
                      f'(same phone) | references: none | delete', out)
        self.assertIn(f'rep {self.v_lee.id} ', out)
        self.assertIn('| keep (active)', out)
        self.assertIn(f'rep {self.v_kim.id} ', out)
        self.assertIn('references: maps.Lead.rep x1 | keep (referenced)', out)
        self.assertIn(f'copies Team Sunshine rep {self.s_ash.id} (same name)', out)
        self.assertNotIn(f'rep {self.v_rep.id} ', out)  # Ventana's own rep copies nobody
        self.assertIn('Candidates: 4; to delete: 2; kept: 2.', out)
        self.assertIn('nothing deleted', out)
        self.assertEqual(self.ids(self.ventana), before_v)
        self.assertEqual(self.ids(self.sunshine), before_s)

    def test_no_full_number_in_the_output(self):
        out = self.run_cmd()
        for number in ('6175550011', '6175550012', '6175550013', '555-0011'):
            self.assertNotIn(number, out)

    def test_every_kind_of_reference_keeps_a_row(self):
        v_pat = self.v_pat
        TimeOffRequest.all_objects.create(rep=v_pat, start_date=date(2026, 11, 2), organization=self.ventana)
        out = self.run_cmd()
        self.assertIn('maps.TimeOffRequest.rep x1 | keep (referenced)', out)
        TimeOffRequest.all_objects.filter(rep=v_pat).delete()
        VoiceCallLog.all_objects.create(rep=v_pat, caller_number='6175550011', organization=self.sunshine)
        self.assertIn('maps.VoiceCallLog.rep x1 | keep (referenced)', self.run_cmd())  # a row in the other org counts
        VoiceCallLog.all_objects.filter(rep=v_pat).delete()
        user = User.objects.create_user('pat', password='pw')
        UserProfile.all_objects.create(user=user, role='rep', rep=v_pat, organization=self.ventana)
        self.assertIn('maps.UserProfile.rep x1 | keep (referenced)', self.run_cmd())


class ApplyTests(CleanupBase):
    def test_deletes_only_the_unreferenced_inactive_ventana_copies(self):
        before_s = self.ids(self.sunshine)
        out = self.run_cmd('--apply')
        self.assertIn(f'Deleted 2 Ventana rep row(s): {self.v_pat.id}, {self.v_ash.id}.', out)
        self.assertIn("Team Sunshine's rep count is unchanged", out)
        remaining = self.ids(self.ventana)
        self.assertNotIn(self.v_pat.id, remaining)
        self.assertNotIn(self.v_ash.id, remaining)
        self.assertTrue({self.v_lee.id, self.v_kim.id, self.v_rep.id} <= remaining)
        self.assertEqual(self.ids(self.sunshine), before_s)
        self.assertEqual(Lead.all_objects.filter(rep=self.v_kim).count(), 1)
        # a second run has nothing left to delete
        self.assertIn('Nothing to delete.', self.run_cmd('--apply'))

    def test_refuses_and_changes_nothing_if_team_sunshines_count_would_change(self):
        before_v = self.ids(self.ventana)
        with mock.patch('maps.management.commands.cleanup_duplicate_reps.sunshine_count', side_effect=[10, 9]):
            with self.assertRaisesRegex(CommandError, "Team Sunshine's rep count would change"):
                self.run_cmd('--apply')
        self.assertEqual(self.ids(self.ventana), before_v)

    def test_refuses_if_a_reference_appears_before_the_delete(self):
        before_v = self.ids(self.ventana)
        real = __import__('maps.management.commands.cleanup_duplicate_reps', fromlist=['references']).references
        calls = {'n': 0}

        def late(rep):
            calls['n'] += 1
            # the listing pass reads the truth; the re-check under the lock sees a new lead
            return real(rep) if calls['n'] <= 4 else [('maps.Lead.rep', 1)]
        with mock.patch('maps.management.commands.cleanup_duplicate_reps.references', side_effect=late):
            with self.assertRaisesRegex(CommandError, 'gained a reference'):
                self.run_cmd('--apply')
        self.assertEqual(self.ids(self.ventana), before_v)
