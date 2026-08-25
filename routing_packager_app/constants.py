from enum import Enum


class Providers(str, Enum):
    OSM = "osm"
    TOMTOM = "tomtom"
    HERE = "here"


class Statuses(str, Enum):
    QUEUED = "Queued"
    COMPRESSING = "Compressing"
    FAILED = "Failed"
    DELETED = "Deleted"
    COMPLETED = "Completed"


class BuildState(str, Enum):
    UNKNOWN = "unknown"
    IDLE = "idle"
    BUILDING = "building"
    FAILED = "failed"


class BuildStage(str, Enum):
    PRUNING = "pruning"
    DOWNLOADING_PBF = "downloading_pbf"
    UPDATING_PBF = "updating_pbf"
    BUILDING_TILES = "building_tiles"
    BUILDING_ELEVATION = "building_elevation"
    ENHANCING_TILES = "enhancing_tiles"
    SWAPPING = "swapping"
