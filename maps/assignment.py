import math
from datetime import datetime, timedelta
from .models import Lead, Rep

APPOINTMENT_DURATION = 90  # minutes (1.5 hours buffer)
WORK_START_HOUR = 9        # 9:00 AM
WORK_END_HOUR = 20         # 8:00 PM
AVG_SPEED_MPH = 30         # average MA driving speed
TARGET_PER_REP = 3
MAX_PER_REP = 5


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


def is_compatible(rep, lead):
    if not rep.specialty or rep.specialty == 'both':
        return True
    if not lead.appointment_type:
        return True
    if lead.appointment_type == 'both':
        return True
    return rep.specialty == lead.appointment_type


def order_stops_nearest_neighbor(start_lat, start_lng, leads):
    if not leads:
        return []
    remaining = list(leads)
    ordered = []
    cur_lat, cur_lng = start_lat, start_lng

    while remaining:
        nearest = min(
            remaining,
            key=lambda l: haversine_miles(cur_lat, cur_lng, l.latitude, l.longitude)
        )
        ordered.append(nearest)
        cur_lat, cur_lng = nearest.latitude, nearest.longitude
        remaining.remove(nearest)

    return ordered


def compute_schedule(rep, ordered_leads, target_date):
    schedule = []
    current_time = datetime(target_date.year, target_date.month, target_date.day,
                            WORK_START_HOUR, 0)
    end_of_day = datetime(target_date.year, target_date.month, target_date.day,
                          WORK_END_HOUR, 0)
    cur_lat, cur_lng = rep.latitude, rep.longitude

    for lead in ordered_leads:
        travel = travel_minutes(cur_lat, cur_lng, lead.latitude, lead.longitude)
        arrival = current_time + timedelta(minutes=travel)

        if arrival + timedelta(minutes=APPOINTMENT_DURATION) > end_of_day:
            return None

        schedule.append((lead, arrival))
        current_time = arrival + timedelta(minutes=APPOINTMENT_DURATION)
        cur_lat, cur_lng = lead.latitude, lead.longitude

    return schedule


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
            ordered = order_stops_nearest_neighbor(rep.latitude, rep.longitude, clusters[rep.id])
            schedule = compute_schedule(rep, ordered, target_date)
            if schedule:
                assignments.append({'rep': rep, 'stops': schedule})
        return {'assignments': assignments, 'unassigned': []}

    # Sort unassigned leads furthest-from-nearest-rep first (prevents orphans)
    def min_compatible_rep_distance(lead):
        dists = []
        for rep in reps:
            if is_compatible(rep, lead):
                d = haversine_miles(rep.latitude, rep.longitude,
                                    lead.latitude, lead.longitude)
                dists.append(d)
        return min(dists) if dists else float('inf')

    remaining_leads = sorted(unassigned_leads, key=min_compatible_rep_distance, reverse=True)
    unassigned = []

    # Greedy assignment: each lead to nearest compatible rep under capacity
    # Locked leads count toward capacity
    # Balance load among same-rated reps to avoid burnout
    def get_min_load_for_rating(rating):
        """Find the minimum cluster size among reps with this rating."""
        return min(
            (len(clusters[r.id]) for r in reps if r.rating == rating),
            default=0,
        )

    for lead in remaining_leads:
        best_rep_id = None
        best_dist = float('inf')

        for rep in reps:
            if not is_compatible(rep, lead):
                continue
            if len(clusters[rep.id]) >= MAX_PER_REP:
                continue
            dist = haversine_miles(rep.latitude, rep.longitude,
                                   lead.latitude, lead.longitude)
            # Soft penalty for reps already at target capacity
            if len(clusters[rep.id]) >= TARGET_PER_REP:
                dist *= 1.5
            # Load-balancing: penalize reps that have more leads than
            # the least-loaded rep at the same rating tier
            min_load = get_min_load_for_rating(rep.rating)
            excess = len(clusters[rep.id]) - min_load
            if excess > 0:
                dist *= (1.0 + 0.3 * excess)
            if dist < best_dist:
                best_dist = dist
                best_rep_id = rep.id

        if best_rep_id is not None:
            clusters[best_rep_id].append(lead)
        else:
            unassigned.append(lead)

    # Order each cluster and validate schedule
    # Locked leads are never dropped — only auto-assigned leads can be dropped
    assignments = []
    for rep in reps:
        cluster_leads = clusters[rep.id]
        if not cluster_leads:
            continue

        ordered = order_stops_nearest_neighbor(rep.latitude, rep.longitude, cluster_leads)

        schedule = compute_schedule(rep, ordered, target_date)
        while schedule is None and len(ordered) > 0:
            # Only drop auto-assigned leads, never locked ones
            drop_candidate = None
            for i in range(len(ordered) - 1, -1, -1):
                if ordered[i].id not in locked_ids:
                    drop_candidate = i
                    break
            if drop_candidate is None:
                # All remaining are locked — can't drop any, keep as-is
                break
            dropped = ordered.pop(drop_candidate)
            unassigned.append(dropped)
            schedule = compute_schedule(rep, ordered, target_date)

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
        'unassigned': unassigned,
    }
