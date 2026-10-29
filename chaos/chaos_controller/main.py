"""Chaos controller: injects faults into the shop and keeps the ground truth.

Used by the Chaos Lab UI and the benchmark runner. Sherlog has no network
route to this service.
"""

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from chaos_controller import faults
from chaos_controller.faults import FaultError
from chaos_controller.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chaos")

SCENARIOS_DIR = Path(os.environ.get("SCENARIOS_DIR", "/scenarios"))
STEP_KEYS = ("type", "target", "service", "proxy", "flag", "delay_s")

store = Store(os.environ.get("CHAOS_DB", "/data/chaos.db"))
pending: set[asyncio.Task] = set()


def load_scenarios() -> dict[str, dict]:
    scenarios = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        scenarios[doc["id"]] = doc
    return scenarios


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Loaded %d scenarios from %s", len(load_scenarios()), SCENARIOS_DIR)
    yield
    for task in pending:
        task.cancel()


app = FastAPI(title="chaos-controller", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class FaultRequest(BaseModel):
    type: str
    target: str
    params: dict = Field(default_factory=dict)


def normalize_step(step: dict) -> tuple[str, str, dict, float]:
    target = step.get("target") or step.get("service") or step.get("proxy") or step.get("flag")
    params = {k: v for k, v in step.items() if k not in STEP_KEYS}
    return step["type"], target, params, float(step.get("delay_s", 0))


async def inject(type_: str, target: str, params: dict, run_id: str | None = None) -> dict:
    if type_ not in faults.TYPES:
        raise HTTPException(status_code=400, detail=f"unknown fault type {type_}")
    fault_id = f"flt_{secrets.token_hex(5)}"
    try:
        undo = await asyncio.to_thread(faults.inject, fault_id, type_, target, params)
    except (FaultError, KeyError) as exc:
        store.add_fault(fault_id, run_id, type_, target, params, {}, "failed", error=str(exc))
        log.error("Fault %s %s on %s failed: %s", fault_id, type_, target, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    store.add_fault(fault_id, run_id, type_, target, params, undo, "active")
    log.info("Injected %s: %s on %s %s", fault_id, type_, target, params)
    return store.fault(fault_id)


# ------------------------------------------------------------------ faults
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/faults/types")
def fault_types():
    return {
        "types": faults.TYPES,
        "services": faults.SERVICES + faults.DATASTORES,
        "proxies": faults.PROXIES,
    }


@app.get("/faults")
def list_faults(status: str | None = "active"):
    return store.faults(status=status)


@app.post("/faults", status_code=201)
async def create_fault(req: FaultRequest):
    return await inject(req.type, req.target, req.params)


@app.delete("/faults/{fault_id}")
async def delete_fault(fault_id: str):
    fault = store.fault(fault_id)
    if fault is None:
        raise HTTPException(status_code=404, detail="unknown fault")
    if fault["status"] != "active":
        return fault
    try:
        await asyncio.to_thread(faults.revert, fault)
    except FaultError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    store.mark_fault(fault_id, "reverted")
    return store.fault(fault_id)


@app.post("/reset")
async def reset():
    """Cancel pending scenario steps and restore the whole system to baseline."""
    for task in list(pending):
        task.cancel()
    try:
        result = await asyncio.to_thread(faults.restore_baseline)
    except FaultError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    for fault in store.faults(status="active"):
        store.mark_fault(fault["id"], "reverted")
    store.end_active_runs()
    log.info("Reset to baseline: %s", result)
    return result


# --------------------------------------------------------------- scenarios
@app.get("/scenarios")
def list_scenarios():
    return list(load_scenarios().values())


@app.post("/scenarios/{scenario_id}/start", status_code=201)
async def start_scenario(scenario_id: str):
    scenario = load_scenarios().get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="unknown scenario")
    if store.faults(status="active"):
        raise HTTPException(status_code=409, detail="faults are already active; reset first")

    run_id = f"run_{secrets.token_hex(5)}"
    store.add_run(run_id, scenario_id, scenario["ground_truth"])
    steps = [normalize_step(step) for step in scenario["inject"]]

    immediate = [s for s in steps if s[3] == 0]
    delayed = [s for s in steps if s[3] > 0]
    injected = [await inject(t, target, params, run_id) for t, target, params, _ in immediate]

    async def later(type_, target, params, delay):
        await asyncio.sleep(delay)
        try:
            await inject(type_, target, params, run_id)
        except HTTPException:
            pass  # already recorded as a failed fault

    for type_, target, params, delay in delayed:
        task = asyncio.create_task(later(type_, target, params, delay))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return {"run_id": run_id, "scenario_id": scenario_id, "injected": injected, "scheduled": len(delayed)}


@app.get("/runs")
def list_runs():
    return store.runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = store.run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown run")
    return {**run, "faults": store.faults(status=None, run_id=run_id)}
