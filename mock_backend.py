"""Lightweight mock SCADA backend — no external dependencies.

Stand-in for a real backend (e.g. graccess-mcp) so the aggregator demo
runs without any AVEVA installation.
"""

import os
import random

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("mock-backend", port=int(os.environ.get("MOCK_PORT", 8003)))


@mcp.tool()
async def get_plant_status() -> str:
    """Get the current operational status of the plant."""
    return (
        "Plant Status: RUNNING\n"
        "Mode: Auto\n"
        "Uptime: 14d 6h 23m\n"
        "Active alarms: 0"
    )


@mcp.tool()
async def list_sensors() -> str:
    """List all sensors with their current simulated readings."""
    temp = round(random.uniform(68.0, 74.0), 1)
    pressure = round(random.uniform(14.2, 15.1), 2)
    flow = round(random.uniform(38.0, 46.0), 1)
    return (
        f"Sensor readings:\n"
        f"  TT-101  Temperature  {temp} °F\n"
        f"  PT-201  Pressure     {pressure} PSI\n"
        f"  FT-301  Flow rate    {flow} GPM"
    )


@mcp.tool()
async def acknowledge_alarm(alarm_id: str, operator: str) -> str:
    """Acknowledge a plant alarm by ID.

    Args:
        alarm_id: Alarm identifier, e.g. "ALM-042".
        operator: Operator name or badge number acknowledging the alarm.
    """
    return f"Alarm {alarm_id} acknowledged by {operator}."


@mcp.tool()
async def set_setpoint(tag: str, value: float, operator: str) -> str:
    """Write a new setpoint to a control tag.

    Args:
        tag:      Tag name, e.g. "TIC-101.SP".
        value:    New setpoint value.
        operator: Operator making the change (for audit trail).
    """
    return f"Setpoint {tag} set to {value} by {operator}. Change logged."


if __name__ == "__main__":
    mcp.run("sse")
