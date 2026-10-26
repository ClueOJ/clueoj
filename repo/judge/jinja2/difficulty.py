from . import registry


# Published at /problem_tags/. These are original problem points, not contest scores.
CLUEOJ_TO_CODEFORCES = {
    5: 800,
    8: 900,
    11: 1000,
    14: 1100,
    17: 1200,
    20: 1300,
    25: 1400,
    30: 1500,
    35: 1600,
    40: 1700,
    45: 1800,
    50: 1900,
    55: 2000,
    60: 2100,
    70: 2200,
    80: 2300,
    90: 2400,
    100: 2500,
    110: 2600,
    120: 2700,
    130: 2800,
    140: 2900,
    150: 3000,
    160: 3100,
    170: 3200,
    180: 3300,
    190: 3400,
    200: 3500,
}


@registry.function
def cf_difficulty(points):
    return CLUEOJ_TO_CODEFORCES.get(points)
