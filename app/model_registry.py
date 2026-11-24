"""Register all application tables on the shared declarative Base."""

from app.models import Base as Base
from app.agent import models as _agent_models
from app.companion import tutor_models as _tutor_models
from app.handover import models as _handover_models
from app.knowledge import models as _knowledge_models
from app.simulation import models as _simulation_models
