# ----------------------------------------------------------------------------------------------------------------------------------------------------
# Purpose: Pydantic Models and SQLModel Tables
# ----------------------------------------------------------------------------------------------------------------------------------------------------

from typing import Optional
from sqlmodel import SQLModel, Field
from pydantic import field_validator, ConfigDict
from datetime import datetime

class JobListing(SQLModel, table=True):
    # This forces Pydantic to validate the data even while assigning values
    # This prevents the SQLModel from bypassing our rules for perfdormance.
    model_config = ConfigDict(validate_assignment=True)

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(min_length=5, max_length=100) # Set length constraints for Title Field
    description: str = Field(min_length=10) # Must be something substantial
    skills: str
    location: str
    salary_range: str | None = None

    created_at: datetime = Field(default_factory=datetime.now)

    # --------------------- Custom validation Logic-----------------------
    @field_validator("salary_range", mode="after")
    @classmethod
    def validate_salary(cls, value: str | None) -> str | None:
        """
        Enforce PRD Rule: No negative numbers in salary
        """
        if value is None:
            return None
        
        # Check if user is trying to enter a negative number
        if value.strip().startswith("-"):
            raise ValueError("Salary cannot be negative. We pay people here!")
        
        return value

class User(SQLModel, table=True):
    """
    Represents a registered user (Recruiter/Admin) in the system.
    """
    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str
    full_name: str | None = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)


class AnalysisRequest(SQLModel):
    text: str



