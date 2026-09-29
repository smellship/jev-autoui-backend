from fastapi import APIRouter

from app.api.v1 import auth, batches, cases, envs, nodes, runs, scenarios, suites, users

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(envs.router)
router.include_router(nodes.router)
router.include_router(scenarios.router)
router.include_router(cases.router)
router.include_router(suites.router)
router.include_router(runs.router)
router.include_router(batches.router)
router.include_router(users.router)

__all__ = ["router"]
