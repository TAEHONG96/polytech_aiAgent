from fastapi import Depends, FastAPI

from app.agent.routes import router as agent_router
from app.auth import require_operator, router as auth_router
from app.companion.routes import router as companion_router
from app.companion.tutor_routes import router as tutor_router
from app.db import router as readiness_router
from app.http import install_http_handlers
from app.handover.routes import router as handover_router
from app.handover.ticket_routes import router as ticket_operations_router
from app.knowledge.routes import router as knowledge_router
from app.simulation.routes import router as simulation_router
from app.simulation.resources import router as training_resources
from app.simulation.handover import router as training_handover
from app.simulation.evaluations import router as training_evaluations
from app.simulation.actions import router as training_actions
from app.simulation.learning import router as training_learning
from app.tickets import router as ticket_router

app = FastAPI(title="IT 운영 지원", version="0.1.0")
install_http_handlers(app)
app.include_router(readiness_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(companion_router, prefix="/api/v1")
app.include_router(tutor_router, prefix="/api/v1")
app.include_router(simulation_router, prefix="/api/v1")
app.include_router(training_resources, prefix="/api/v1")
app.include_router(training_handover, prefix="/api/v1")
app.include_router(training_evaluations, prefix="/api/v1")
app.include_router(training_actions, prefix="/api/v1")
app.include_router(training_learning, prefix="/api/v1")
for operations_router in (ticket_router, knowledge_router, agent_router, handover_router, ticket_operations_router):
    app.include_router(operations_router, prefix="/api/v1", dependencies=[Depends(require_operator)])


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
