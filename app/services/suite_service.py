"""场景套件：命名 + 有序用例列表（场景管理）。"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import ApiError, not_found
from app.db import next_seq
from app.models import Case, Suite, SuiteCase, utcnow
from app.services import audit


def get_suite(db: Session, code: str) -> Suite:
    suite = db.scalar(select(Suite).where(Suite.code == code, Suite.deleted_at.is_(None)))
    if suite is None:
        raise not_found("套件", code)
    return suite


def refs_of(db: Session, suite_id: int) -> list[SuiteCase]:
    return list(db.scalars(select(SuiteCase).where(SuiteCase.suite_id == suite_id)
                           .order_by(SuiteCase.sort, SuiteCase.id)).all())


def cases_of(db: Session, suite_id: int) -> tuple[list[Case], list[Case]]:
    """返回 (可用用例, 已被删的引用)——后者供运行/编辑前提示先修套件。"""
    refs = refs_of(db, suite_id)
    case_ids = [ref.case_id for ref in refs]
    if not case_ids:
        return [], []
    rows = db.scalars(select(Case).where(Case.id.in_(case_ids))).all()
    by_id = {case.id: case for case in rows}
    live: list[Case] = []
    missing: list[Case] = []
    for ref in refs:
        case = by_id.get(ref.case_id)
        if case is None:
            continue
        (missing if case.deleted_at is not None else live).append(case)
    return live, missing


def set_cases(db: Session, suite: Suite, case_codes: list[str], user) -> int:
    """整表覆盖：按传入顺序落 sort；重复按首次出现去重；不存在/已删用例 409。"""
    seen: set[str] = set()
    codes: list[str] = []
    for code in case_codes:
        if code not in seen:
            seen.add(code)
            codes.append(code)
    rows = db.scalars(select(Case).where(Case.code.in_(codes))).all() if codes else []
    by_code = {case.code: case for case in rows}
    invalid = [code for code in codes if code not in by_code or by_code[code].deleted_at is not None]
    if invalid:
        raise ApiError(409, "case_invalid", "有用例不存在或已删除，套件未保存", {"cases": invalid})
    db.execute(delete(SuiteCase).where(SuiteCase.suite_id == suite.id))
    for index, code in enumerate(codes):
        db.add(SuiteCase(suite_id=suite.id, case_id=by_code[code].id, sort=index))
    suite.updated_at = utcnow()
    audit.log(db, user, "suite.set_cases", "suite", suite.code, {"count": len(codes)})
    db.commit()
    return len(codes)


def create_suite(db: Session, name: str, owner: str, tags: list[str], description: str,
                 user) -> Suite:
    suite = Suite(code=f"su-{next_seq(db, 'suite'):04d}", name=name, owner=owner,
                  tags=tags or [], description=description)
    db.add(suite)
    db.flush()
    audit.log(db, user, "suite.create", "suite", suite.code, {"name": suite.name})
    db.commit()
    return suite


def copy_suite(db: Session, suite: Suite, name: str, user) -> Suite:
    new_suite = Suite(code=f"su-{next_seq(db, 'suite'):04d}", name=name, owner=suite.owner,
                      tags=list(suite.tags or []), description=suite.description)
    db.add(new_suite)
    db.flush()
    for ref in refs_of(db, suite.id):
        db.add(SuiteCase(suite_id=new_suite.id, case_id=ref.case_id, sort=ref.sort))
    audit.log(db, user, "suite.copy", "suite", new_suite.code, {"from": suite.code})
    db.commit()
    return new_suite
