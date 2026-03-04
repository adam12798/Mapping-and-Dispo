import math
from datetime import datetime, timedelta
from .models import Lead, Rep

APPOINTMENT_DURATION = 90  # minutes (1.5 hours buffer)
WORK_START_HOUR = 9        # 9:00 AM
WORK_END_HOUR = 17         # 5:00 PM
AVG_SPEED_MPH = 30         # average MA driving speed
TARGET_PER_REP = 4
MAX_PER_REP = 6


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
    leads = list(Lead.objects.filter(
        appointment_datetime__date=target_date,
        latitude__isnull=False,
        longitude__isnull=False,
        rep__isnull=True,
    ))

    reps = list(Rep.objects.filter(
        latitude__isnull=False,
        longitude__isnull=False,
    ).order_by('-rating'))

    if not reps:
        return {'assignments': [], 'unassigned': leads}

    if not leads:
        return {'assignments': [], 'unassigned': []}

    # Initialize clusters
    clusters = {rep.id: [] for rep in reps}

    # Sort leads furthest-from-nearest-rep first (prevents orphans)
    def min_compatible_rep_distance(lead):
        dists = []
        for rep in reps:
            if is_compatible(rep, lead):
                d = haversine_miles(rep.latitude, rep.longitude,
                                    lead.latitude, lead.longitude)
                dists.append(d)
        return min(dists) if dists else float('inf')

    remaining_leads = sorted(leads, key=min_compatible_rep_distance, reverse=True)
    unassigned = []

    # Greedy assignment: each lead to nearest compatible rep under capacity
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
            if dist < best_dist:
                best_dist = dist
                best_rep_id = rep.id

        if best_rep_id is not None:
            clusters[best_rep_id].append(lead)
        else:
            unassigned.append(lead)

    # Order each cluster and validate schedule
    assignments = []
    for rep in reps:
        cluster_leads = clusters[rep.id]
        if not cluster_leads:
            continue

        ordered = order_stops_nearest_neighbor(rep.latitude, rep.longitude, cluster_leads)

        schedule = compute_schedule(rep, ordered, target_date)
        while schedule is None and len(ordered) > 0:
            dropped = ordered.pop()
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
                lead.rep = rep
                lead.save(update_fields=['rep_id'])

    return {
        'assignments': assignments,
        'unassigned': unassigned,
    }
