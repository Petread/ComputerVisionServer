from pydantic import BaseModel, ConfigDict, Field


class AreaWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)


class FacilityWrite(AreaWrite):
    area_id: str = Field(min_length=1, max_length=64)


class AreaDetail(AreaWrite):
    id: str


class FacilityDetail(FacilityWrite):
    id: str


class PlaceList(BaseModel):
    areas: list[AreaDetail]
    facilities: list[FacilityDetail]
