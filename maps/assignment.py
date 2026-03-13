import math
from datetime import datetime, timedelta
from itertools import permutations
from .models import Lead, Rep, TimeOffRequest

APPOINTMENT_DURATION = 90   # minutes per appointment
WORK_START_HOUR = 8         # 8:00 AM
WORK_END_HOUR = 22          # 10:00 PM
AVG_SPEED_MPH = 45          # average MA driving speed (haversine, not road distance)
TARGET_PER_REP = 3
MAX_PER_REP = 5
LATE_WINDOW = 30            # minutes — max acceptable lateness
LATE_STRETCH = 60           # minutes — absolute max (deprioritized)


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def travel_minutes(lat1, lon1, lat2, lon2):
    dist = haversine_miles(lat1, lon1, lat2, lon2)
    return (dist / AVG_SPEED_MPH) * 60


def get_rep_time_off(rep_id, target_date):
    """Get approved time off blocks for a rep on a given date.

    Returns list of (start_datetime, end_datetime) tuples.
    Full day off returns a block covering the entire work window.
    """
    blocks = []
    requests = TimeOffRequest.objects.filter(
        rep_id=rep_id,
        date=target_date,
        status='approved',
    )
    for req in requests:
        if req.start_time and req.end_time:
            start = datetime.combine(target_date, req.start_time)
            end = datetime.combine(target_date, req.end_time)
        else:
            # Full day off
            start = datetime(target_date.year, target_date.month, target_date.day, 0, 0)
            end = datetime(target_date.year, target_date.month, target_date.day, 23, 59)
        blocks.append((start, end))
    return blocks


def is_blocked_by_time_off(arrival, duration_min, time_off_blocks):
    """Check if an appointment window overlaps any time off block."""
    appt_end = arrival + timedelta(minutes=duration_min)
    for block_start, block_end in time_off_blocks:
        if arrival < block_end and appt_end > block_start:
            return True
    return False


def is_compatible(rep, lead):
    if not lead.appointment_type:
        return False  # No type set — can't assign any rep
    if not rep.specialty or rep.specialty == 'both':
        return True
    if lead.appointment_type == 'both':
        return True
    return rep.specialty == lead.appointment_type


def can_rep_make_it(rep_lat, rep_lng, free_at, lead):
    """Check if a rep can reach a lead's appointment within the late window.

    Returns (lateness_minutes, travel_minutes) or None if impossible.
    lateness_minutes: 0 = on time or early, >0 = minutes late.
    """
    travel = travel_minutes(rep_lat, rep_lng, lead.latitude, lead.longitude)
    if not lead.appointment_datetime:
        return (0, travel)

    appt_time = lead.appointment_datetime.replace(tzinfo=None)
    earliest_arrival = free_at + timedelta(minutes=travel)

    # How late would the rep be?
    if earliest_arrival <= appt_time:
        # On time or early — arrives at appt_time
        return (0, travel)

    lateness = (earliest_arrival - appt_time).total_seconds() / 60
    if lateness <= LATE_STRETCH:
        return (lateness, travel)

    return None  # Too late


def score_schedule(schedule, rep):
    """Score a schedule. Lower is better.

    Prioritizes:
    1. Number of appointments covered (more = better, so negative weight)
    2. Total lateness (less = better)
    3. Total driving distance (less = better)
    """
    total_lateness = 0
    total_drive = 0
    cur_lat, cur_lng = rep.latitude, rep.longitude

    for lead, arrival in schedule:
        drive = travel_minutes(cur_lat, cur_lng, lead.latitude, lead.longitude)
        total_drive += drive

        if lead.appointment_datetime:
            appt_time = lead.appointment_datetime.replace(tzinfo=None)
            if arrival > appt_time:
                lateness = (arrival - appt_time).total_seconds() / 60
                # Heavier penalty past the 30-min comfort window
                if lateness > LATE_WINDOW:
                    total_lateness += lateness * 3
                else:
                    total_lateness += lateness

        cur_lat, cur_lng = lead.latitude, lead.longitude

    # Coverage is king: -1000 per appointment covered
    coverage_score = -1000 * len(schedule)
    return coverage_score + total_lateness * 2 + total_drive


def build_best_schedule(rep, leads, target_date, time_off_blocks=None):
    """Find the best ordering of leads for a rep.

    For small sets (<=6), try all permutations.
    For larger sets, use appointment-time ordering with nearest-neighbor tiebreak.
    Skips any appointments that overlap with approved time off blocks.
    """
    if time_off_blocks is None:
        time_off_blocks = get_rep_time_off(rep.id, target_date)

    # If rep has full-day off, they can't take any appointments
    for block_start, block_end in time_off_blocks:
        if block_start.hour == 0 and block_end.hour == 23:
            return []

    end_of_day = datetime(target_date.year, target_date.month, target_date.day,
                          WORK_END_HOUR, 0)

    def try_schedule(ordered_leads):
        schedule = []
        current_time = datetime(target_date.year, target_date.month, target_date.day,
                                WORK_START_HOUR, 0)
        cur_lat, cur_lng = rep.latitude, rep.longitude

        for lead in ordered_leads:
            travel = travel_minutes(cur_lat, cur_lng, lead.latitude, lead.longitude)
            earliest_arrival = current_time + timedelta(minutes=travel)

            if lead.appointment_datetime:
                appt_time = lead.appointment_datetime.replace(tzinfo=None)
                arrival = max(earliest_arrival, appt_time)
                lateness = (earliest_arrival - appt_time).total_seconds() / 60 if earliest_arrival > appt_time else 0
                if lateness > LATE_STRETCH:
                    continue  # Skip — can't make it
            else:
                arrival = earliest_arrival

            if arrival + timedelta(minutes=APPOINTMENT_DURATION) > end_of_day:
                continue  # Skip — won't finish before end of day

            # Skip if appointment overlaps with time off
            if is_blocked_by_time_off(arrival, APPOINTMENT_DURATION, time_off_blocks):
                continue

            schedule.append((lead, arrival))
            current_time = arrival + timedelta(minutes=APPOINTMENT_DURATION)
            cur_lat, cur_lng = lead.latitude, lead.longitude

        return schedule

    if len(leads) <= 6:
        # Try all permutations to find optimal coverage + minimal driving
        best_schedule = []
        best_score = float('inf')
        for perm in permutations(leads):
            sched = try_schedule(list(perm))
            s = score_schedule(sched, rep)
            if s < best_score:
                best_score = s
                best_schedule = sched
        return best_schedule
    else:
        # Heuristic: sort by appointment time, tiebreak by proximity
        sorted_leads = sorted(leads, key=lambda l: (
            l.appointment_datetime.replace(tzinfo=None) if l.appointment_datetime else datetime.max,
        ))
        return try_schedule(sorted_leads)


def auto_assign_leads(target_date, save=True):
    # Unassigned leads to distribute
    unassigned_leads = list(Lead.objects.filter(
        appointment_datetime__date=target_date,
        latitude__isnull=False,
        longitude__isnull=False,
        rep__isnull=True,
    ))

    # Pre-assigned (locked) leads — these are non-negotiable
    locked_leads = list(Lead.objects.filter(
        appointment_datetime__date=target_date,
        latitude__isnull=False,
        longitude__isnull=False,
        rep__isnull=False,
    ).select_related('rep'))

    reps = list(Rep.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
        is_active=True,
    ).order_by('-rating'))

    if not reps:
        return {'assignments': [], 'unassigned': unassigned_leads}

    # Load approved time off blocks for each rep on this date
    rep_time_off = {rep.id: get_rep_time_off(rep.id, target_date) for rep in reps}

    # Filter out reps who have full-day off
    available_reps = []
    for rep in reps:
        full_day_off = any(
            s.hour == 0 and e.hour == 23
            for s, e in rep_time_off[rep.id]
        )
        if not full_day_off:
            available_reps.append(rep)

    # Initialize clusters with locked leads
    clusters = {rep.id: [] for rep in reps}
    locked_ids = set()
    for lead in locked_leads:
        if lead.rep_id in clusters:
            clusters[lead.rep_id].append(lead)
            locked_ids.add(lead.id)

    if not unassigned_leads:
        # Still build routes for locked leads
        assignments = []
        for rep in reps:
            if not clusters[rep.id]:
                continue
            schedule = build_best_schedule(rep, clusters[rep.id], target_date, rep_time_off.get(rep.id, []))
            if schedule:
                assignments.append({'rep': rep, 'stops': schedule})
        return {'assignments': assignments, 'unassigned': []}

    # --- Assignment strategy ---
    # For each lead, figure out which reps can reach it on time (or close).
    # Assign leads to maximize total coverage, then minimize driving.

    # Sort leads by appointment time — earlier appts get assigned first
    # so reps' schedules build forward naturally
    sorted_leads = sorted(unassigned_leads, key=lambda l: (
        l.appointment_datetime.replace(tzinfo=None) if l.appointment_datetime else datetime.max,
    ))

    unassigned = []

    def get_rep_free_time_and_location(rep_id):
        """Where will this rep be and when will they be free after their current stops?"""
        rep_obj = next(r for r in reps if r.id == rep_id)
        if not clusters[rep_id]:
            free_at = datetime(target_date.year, target_date.month, target_date.day,
                               WORK_START_HOUR, 0)
            return rep_obj.latitude, rep_obj.longitude, free_at

        # Build a quick schedule to find last stop's end time
        schedule = build_best_schedule(rep_obj, clusters[rep_id], target_date, rep_time_off.get(rep_id, []))
        if schedule:
            last_lead, last_arrival = schedule[-1]
            free_at = last_arrival + timedelta(minutes=APPOINTMENT_DURATION)
            return last_lead.latitude, last_lead.longitude, free_at
        else:
            free_at = datetime(target_date.year, target_date.month, target_date.day,
                               WORK_START_HOUR, 0)
            return rep_obj.latitude, rep_obj.longitude, free_at

    for lead in sorted_leads:
        best_rep_id = None
        best_lateness = float('inf')
        best_drive = float('inf')

        for rep in available_reps:
            if not is_compatible(rep, lead):
                continue
            if len(clusters[rep.id]) >= MAX_PER_REP:
                continue

            lat, lng, free_at = get_rep_free_time_and_location(rep.id)
            result = can_rep_make_it(lat, lng, free_at, lead)

            if result is None:
                continue

            lateness, drive = result

            # Check if appointment would overlap rep's time off
            if lead.appointment_datetime:
                appt_time = lead.appointment_datetime.replace(tzinfo=None)
                arrival = max(free_at + timedelta(minutes=drive), appt_time)
            else:
                arrival = free_at + timedelta(minutes=drive)
            if is_blocked_by_time_off(arrival, APPOINTMENT_DURATION, rep_time_off[rep.id]):
                continue

            # Prefer: on-time > within 30min late > stretch
            # Among same lateness tier, prefer less driving
            lateness_tier = 0 if lateness == 0 else (1 if lateness <= LATE_WINDOW else 2)
            current_best_tier = 0 if best_lateness == 0 else (1 if best_lateness <= LATE_WINDOW else 2)

            # Load balancing: soft penalty for overloaded reps
            load_penalty = max(0, len(clusters[rep.id]) - TARGET_PER_REP) * 10

            if (lateness_tier < current_best_tier or
                (lateness_tier == current_best_tier and drive + load_penalty < best_drive)):
                best_rep_id = rep.id
                best_lateness = lateness
                best_drive = drive + load_penalty

        if best_rep_id is not None:
            clusters[best_rep_id].append(lead)
        else:
            unassigned.append(lead)

    # Second pass: assign any still-unassigned leads to best available rep
    # regardless of lateness — every appointment should be covered if possible
    if unassigned:
        for lead in list(unassigned):
            best_rep_id = None
            best_drive = float('inf')

            for rep in available_reps:
                if not is_compatible(rep, lead):
                    continue
                if len(clusters[rep.id]) >= MAX_PER_REP:
                    continue

                lat, lng, free_at = get_rep_free_time_and_location(rep.id)
                drive = travel_minutes(lat, lng, lead.latitude, lead.longitude)
                arrival = free_at + timedelta(minutes=drive)

                # Just needs to arrive before end of work day
                end_of_day = datetime(target_date.year, target_date.month, target_date.day,
                                      WORK_END_HOUR, 0)
                if arrival + timedelta(minutes=APPOINTMENT_DURATION) > end_of_day:
                    continue
                if is_blocked_by_time_off(arrival, APPOINTMENT_DURATION, rep_time_off[rep.id]):
                    continue

                if drive < best_drive:
                    best_rep_id = rep.id
                    best_drive = drive

            if best_rep_id is not None:
                clusters[best_rep_id].append(lead)
                unassigned.remove(lead)

    # Build final optimized schedules for each rep
    assignments = []
    still_unassigned = list(unassigned)

    for rep in reps:
        cluster_leads = clusters[rep.id]
        if not cluster_leads:
            continue

        schedule = build_best_schedule(rep, cluster_leads, target_date, rep_time_off.get(rep.id, []))

        # Any leads that didn't make the schedule go back to unassigned
        scheduled_ids = {lead.id for lead, _ in schedule}
        for lead in cluster_leads:
            if lead.id not in scheduled_ids and lead.id not in locked_ids:
                still_unassigned.append(lead)

        if schedule:
            assignments.append({
                'rep': rep,
                'stops': schedule,
            })

    if save:
        for assignment in assignments:
            rep = assignment['rep']
            for lead, arrival_time in assignment['stops']:
                if lead.id not in locked_ids:
                    lead.rep = rep
                    lead.save(update_fields=['rep_id'])

    return {
        'assignments': assignments,
        'unassigned': still_unassigned,
    }
