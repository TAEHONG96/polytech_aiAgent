"""Install the versioned synthetic SOP through the existing document/index workflow."""
import argparse
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.http import ApiError
from app.knowledge.documents import original_path, register_sop
from app.knowledge.models import SopDocument
from app.models import User

TITLE = "합성 데이터센터 메모리 진단 · dco-basic 1.1"
FILENAME = "dco-basic-1.1-memory-check.md"
PAYLOAD = """# 가상 메모리 ECC 경보

## 증상과 점검
실습 서버에서 MEM_ECC_ERROR 표시와 가상 BMC 진단 결과를 확인한다. 메모리
점검에서 오류가 확인되면 실습 카탈로그의 가상 메모리 교체를 고려한다.

## 결과 검증
가상 교체 후 재진단으로 상태와 오류 코드를 다시 확인한다. 미해결이면
관찰한 결과와 다음 조치를 티켓 작업 또는 인계 초안에 기록한다.
""".encode()


def seed(db: Session, operator: str) -> SopDocument:
    actor = db.scalar(select(User.id).where(User.username == operator.casefold(), User.role == "operator",
                                            User.is_active.is_(True)))
    if actor is None:
        raise ValueError("An active operator account is required to seed the synthetic SOP.")
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    document = db.scalar(select(SopDocument).where(SopDocument.content_sha256 == digest))
    if document is None:
        try:
            document, _ = register_sop(db, actor_id=actor, filename=FILENAME, content_type="text/markdown",
                                       payload=PAYLOAD, title=TITLE)
        except ApiError as exc:
            if exc.code != "SOP_DUPLICATE":
                raise
            document = db.scalar(select(SopDocument).where(SopDocument.content_sha256 == digest))
    if (document is None or document.title != TITLE or document.filename != FILENAME
            or original_path(document.id).read_bytes() != PAYLOAD):
        raise ValueError("The existing SOP does not match the approved synthetic seed.")
    if not document.training_approved:
        document.training_approved = True
        db.commit()
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator", required=True)
    args = parser.parse_args()
    with Session(engine()) as session:
        print(seed(session, args.operator).id)
