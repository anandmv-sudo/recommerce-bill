"""Maps full Indian state/UT names (as likely to appear in an excel sheet) to
the state codes Zoho Books actually stores on a contact's place_of_contact
field (confirmed empirically: Telangana -> "TS").
"""

STATE_NAME_TO_CODE = {
    "andaman and nicobar islands": "AN",
    "andhra pradesh": "AP",
    "arunachal pradesh": "AR",
    "assam": "AS",
    "bihar": "BR",
    "chandigarh": "CH",
    "chhattisgarh": "CG",
    "dadra and nagar haveli and daman and diu": "DD",
    "delhi": "DL",
    "goa": "GA",
    "gujarat": "GJ",
    "haryana": "HR",
    "himachal pradesh": "HP",
    "jammu and kashmir": "JK",
    "jharkhand": "JH",
    "karnataka": "KA",
    "kerala": "KL",
    "ladakh": "LA",
    "lakshadweep": "LD",
    "madhya pradesh": "MP",
    "maharashtra": "MH",
    "manipur": "MN",
    "meghalaya": "ME",
    "mizoram": "MI",
    "nagaland": "NL",
    "odisha": "OR",
    "puducherry": "PY",
    "punjab": "PB",
    "rajasthan": "RJ",
    "sikkim": "SK",
    "tamil nadu": "TN",
    "telangana": "TS",
    "tripura": "TR",
    "uttar pradesh": "UP",
    "uttarakhand": "UK",
    "west bengal": "WB",
}


def to_state_code(state: str) -> str:
    """Returns the Zoho state code for a state name, or the input unchanged
    if it already looks like a 2-letter code or isn't in the map."""
    normalized = state.strip().lower()
    return STATE_NAME_TO_CODE.get(normalized, state.strip().upper())


# GST numeric jurisdiction codes (first two digits of a GSTIN / the
# fromStateCode-style fields on an E-way Bill), mapped to the state/UT name
# as printed on official E-way Bill documents (upper case, matching the
# GetEwayBill sample output -- e.g. state code 36 -> "TELANGANA").
GST_STATE_CODE_TO_NAME = {
    "01": "JAMMU AND KASHMIR",
    "02": "HIMACHAL PRADESH",
    "03": "PUNJAB",
    "04": "CHANDIGARH",
    "05": "UTTARAKHAND",
    "06": "HARYANA",
    "07": "DELHI",
    "08": "RAJASTHAN",
    "09": "UTTAR PRADESH",
    "10": "BIHAR",
    "11": "SIKKIM",
    "12": "ARUNACHAL PRADESH",
    "13": "NAGALAND",
    "14": "MANIPUR",
    "15": "MIZORAM",
    "16": "TRIPURA",
    "17": "MEGHALAYA",
    "18": "ASSAM",
    "19": "WEST BENGAL",
    "20": "JHARKHAND",
    "21": "ODISHA",
    "22": "CHHATTISGARH",
    "23": "MADHYA PRADESH",
    "24": "GUJARAT",
    "25": "DAMAN AND DIU",
    "26": "DADRA AND NAGAR HAVELI AND DAMAN AND DIU",
    "27": "MAHARASHTRA",
    "28": "ANDHRA PRADESH (OLD)",
    "29": "KARNATAKA",
    "30": "GOA",
    "31": "LAKSHADWEEP",
    "32": "KERALA",
    "33": "TAMIL NADU",
    "34": "PUDUCHERRY",
    "35": "ANDAMAN AND NICOBAR ISLANDS",
    "36": "TELANGANA",
    "37": "ANDHRA PRADESH",
    "38": "LADAKH",
}


def gst_state_code_to_name(code) -> str:
    """Returns the state/UT name for a numeric GST jurisdiction code
    (e.g. 36 or "36" -> "TELANGANA"), or the input unchanged if unknown."""
    key = str(code).strip().zfill(2)
    return GST_STATE_CODE_TO_NAME.get(key, str(code))
