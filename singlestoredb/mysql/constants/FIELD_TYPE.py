DECIMAL = 0
TINY = 1
SHORT = 2
LONG = 3
FLOAT = 4
DOUBLE = 5
NULL = 6
TIMESTAMP = 7
LONGLONG = 8
INT24 = 9
DATE = 10
TIME = 11
DATETIME = 12
YEAR = 13
NEWDATE = 14
VARCHAR = 15
BIT = 16
JSON = 245
NEWDECIMAL = 246
ENUM = 247
SET = 248
TINY_BLOB = 249
MEDIUM_BLOB = 250
LONG_BLOB = 251
BLOB = 252
VAR_STRING = 253
STRING = 254
GEOMETRY = 255

CHAR = TINY
INTERVAL = ENUM
BOOL = TINY

# SingleStoreDB-specific.
# Only enabled when enable_extended_types_metadata=1 in the server.
BSON = 1001
FLOAT32_VECTOR_JSON = 2001
FLOAT64_VECTOR_JSON = 2002
INT8_VECTOR_JSON = 2003
INT16_VECTOR_JSON = 2004
INT32_VECTOR_JSON = 2005
INT64_VECTOR_JSON = 2006
FLOAT32_VECTOR = 3001
FLOAT64_VECTOR = 3002
INT8_VECTOR = 3003
INT16_VECTOR = 3004
INT32_VECTOR = 3005
INT64_VECTOR = 3006
