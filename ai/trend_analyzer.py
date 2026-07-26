from statistics import mean


TAG_UNITS = {
    "CompressorPressure": "bar",
    "CompressorTemperature": "°C",
    "CompressorCurrent": "A",
    "CompressorRunning": "",
    "CompressorWarning": "",
    "CompressorAlarm": "",
    "ChillerSupplyTemp": "°C",
    "ChillerReturnTemp": "°C",
    "ColdRoomTemperature": "°C",
    "ColdRoomHumidity": "%",
    "ColdRoomDoor": "",
    "TankLevel": "%",
    "TransferPumpRunning": "",
    "TransferPumpCurrent": "A",
    "FactoryEnergyKW": "kW",
    "WaterPressure": "bar",
    "AirPressure": "bar",
}


DIGITAL_TAGS = {
    "CompressorRunning",
    "CompressorWarning",
    "CompressorAlarm",
    "ColdRoomDoor",
    "TransferPumpRunning",
}


def format_number(value):
    if value is None:
        return "N/A"

    return f"{value:.2f}".rstrip("0").rstrip(".")


def digital_state(value):
    try:
        return "ON" if float(value) != 0 else "OFF"
    except (TypeError, ValueError):
        return str(value)


def calculate_analogue_trend(values):
    if len(values) < 2:
        return "No trend history"

    first_value = values[0]
    latest_value = values[-1]
    change = latest_value - first_value

    average_magnitude = max(abs(mean(values)), 1.0)

    stable_threshold = average_magnitude * 0.01
    rapid_threshold = average_magnitude * 0.10

    if abs(change) <= stable_threshold:
        return "Stable"

    if change >= rapid_threshold:
        return "Rising rapidly"

    if change > stable_threshold:
        return "Rising"

    if change <= -rapid_threshold:
        return "Falling rapidly"

    return "Falling"


def calculate_digital_trend(values):
    if len(values) < 2:
        return "No trend history"

    transitions = 0

    for previous, current in zip(values, values[1:]):
        previous_state = float(previous) != 0
        current_state = float(current) != 0

        if previous_state != current_state:
            transitions += 1

    if transitions == 0:
        return "No state change"

    return f"{transitions} state transition(s)"


def analyse_tag(tag, rows):
    latest_row = rows[-1]
    unit = TAG_UNITS.get(tag, "")

    numeric_values = []

    for row in rows:
        try:
            numeric_values.append(float(row["value"]))
        except (TypeError, ValueError):
            pass

    if tag in DIGITAL_TAGS:
        return {
            "tag": tag,
            "address": latest_row["address"],
            "updated": latest_row["time"],
            "current": digital_state(latest_row["value"]),
            "unit": "",
            "samples": len(rows),
            "trend": calculate_digital_trend(numeric_values),
            "average": None,
            "minimum": None,
            "maximum": None,
            "change": None,
        }

    if not numeric_values:
        return {
            "tag": tag,
            "address": latest_row["address"],
            "updated": latest_row["time"],
            "current": latest_row["value"],
            "unit": unit,
            "samples": len(rows),
            "trend": "Unavailable",
            "average": None,
            "minimum": None,
            "maximum": None,
            "change": None,
        }

    return {
        "tag": tag,
        "address": latest_row["address"],
        "updated": latest_row["time"],
        "current": numeric_values[-1],
        "unit": unit,
        "samples": len(numeric_values),
        "trend": calculate_analogue_trend(numeric_values),
        "average": mean(numeric_values),
        "minimum": min(numeric_values),
        "maximum": max(numeric_values),
        "change": numeric_values[-1] - numeric_values[0],
    }


def analyse_history(history):
    summaries = []

    for tag in sorted(history):
        summaries.append(analyse_tag(tag, history[tag]))

    return summaries
