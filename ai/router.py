EQUIPMENT_TAGS = {
    "compressor": [
        "CompressorPressure",
        "CompressorTemperature",
        "CompressorCurrent",
        "CompressorRunning",
        "CompressorWarning",
        "CompressorAlarm",
        "AirPressure",
    ],
    "chiller": [
        "ChillerSupplyTemp",
        "ChillerReturnTemp",
    ],
    "cold_room": [
        "ColdRoomTemperature",
        "ColdRoomHumidity",
        "ColdRoomDoor",
    ],
    "tank": [
        "TankLevel",
        "TransferPumpRunning",
        "TransferPumpCurrent",
    ],
    "pump": [
        "TransferPumpRunning",
        "TransferPumpCurrent",
        "TankLevel",
    ],
    "energy": [
        "FactoryEnergyKW",
    ],
    "water": [
        "WaterPressure",
    ],
    "air": [
        "AirPressure",
        "CompressorPressure",
        "CompressorRunning",
        "CompressorWarning",
        "CompressorAlarm",
    ],
}


FACTORY_WIDE_WORDS = {
    "factory",
    "everything",
    "all equipment",
    "all machines",
    "anything abnormal",
    "overall",
    "whole plant",
    "plant status",
}


TREND_WORDS = {
    "trend",
    "rising",
    "falling",
    "increasing",
    "decreasing",
    "history",
    "historical",
    "performance",
    "performing",
    "getting worse",
    "getting better",
}


STATUS_WORDS = {
    "abnormal",
    "alarm",
    "warning",
    "problem",
    "fault",
    "healthy",
    "health",
    "condition",
    "status",
    "maintenance",
}


def detect_intent(question):
    text = question.lower()

    if any(word in text for word in TREND_WORDS):
        return "trend"

    if any(word in text for word in STATUS_WORDS):
        return "status"

    return "current"


def detect_equipment(question):
    text = question.lower()

    if any(word in text for word in FACTORY_WIDE_WORDS):
        return "factory"

    if "cold room" in text or "coldroom" in text:
        return "cold_room"

    if "compressor" in text:
        return "compressor"

    if "chiller" in text:
        return "chiller"

    if "tank" in text:
        return "tank"

    if "pump" in text:
        return "pump"

    if "energy" in text or "power" in text or "kilowatt" in text:
        return "energy"

    if "water" in text:
        return "water"

    if "air pressure" in text or "compressed air" in text:
        return "air"

    return "factory"


def route_question(question):
    equipment = detect_equipment(question)
    intent = detect_intent(question)

    if equipment == "factory":
        tags = None
    else:
        tags = EQUIPMENT_TAGS[equipment]

    if intent == "current":
        history_limit = 1
    elif intent == "trend":
        history_limit = 20
    else:
        history_limit = 10

    return {
        "equipment": equipment,
        "intent": intent,
        "tags": tags,
        "history_limit": history_limit,
    }
