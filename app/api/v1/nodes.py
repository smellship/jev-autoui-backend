"""分类树（§8.2）。"""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Db
from app.api.serializers import node_brief, node_tree
from app.core.errors import ApiError, not_found
from app.models import Node, Scenario, utcnow
from app.schemas import NodeIn, NodePatch
from app.services import audit

router = APIRouter(prefix="/nodes", tags=["nodes"])


def get_node(db: Session, node_id: int) -> Node:
    node = db.get(Node, node_id)
    if node is None or node.deleted_at is not None:
        raise not_found("节点", node_id)
    return node


@router.get("")
def list_nodes(user: CurrentUser, db: Db) -> dict:
    nodes = db.scalars(select(Node).where(Node.deleted_at.is_(None))
                       .order_by(Node.sort, Node.id)).all()
    return {"items": node_tree(list(nodes))}


@router.post("", status_code=201)
def create_node(body: NodeIn, user: CurrentUser, db: Db) -> dict:
    if body.parent_id is not None:
        parent = db.get(Node, body.parent_id)
        if parent is None or parent.deleted_at is not None:
            raise not_found("父节点", body.parent_id)
    node = Node(parent_id=body.parent_id, name=body.name, sort=body.sort,
                description=body.description)
    db.add(node)
    db.flush()
    audit.log(db, user, "node.create", "node", node.id, {"name": node.name})
    db.commit()
    return node_brief(node)


@router.patch("/{node_id}")
def patch_node(node_id: int, body: NodePatch, user: CurrentUser, db: Db) -> dict:
    node = get_node(db, node_id)
    if body.parent_id is not None and body.parent_id != node.parent_id:
        parent = db.get(Node, body.parent_id)
        if parent is None or parent.deleted_at is not None:
            raise not_found("父节点", body.parent_id)
        if body.parent_id == node.id:
            raise ApiError(422, "bad_parent", "节点不能以自己为父节点")
        node.parent_id = body.parent_id
    if body.name is not None:
        node.name = body.name
    if body.sort is not None:
        node.sort = body.sort
    if body.description is not None:
        node.description = body.description
    audit.log(db, user, "node.update", "node", node.id, {"name": node.name})
    db.commit()
    return node_brief(node)


@router.delete("/{node_id}", status_code=204)
def delete_node(node_id: int, user: CurrentUser, db: Db) -> Response:
    node = get_node(db, node_id)
    children = db.scalar(select(Node).where(Node.parent_id == node.id, Node.deleted_at.is_(None)))
    if children is not None:
        raise ApiError(409, "node_not_empty", "该节点下还有子节点，先处理子节点再删除")
    scenarios = db.scalar(select(Scenario).where(Scenario.node_id == node.id,
                                                 Scenario.deleted_at.is_(None)))
    if scenarios is not None:
        raise ApiError(409, "node_not_empty", "该节点下还有场景，先处理场景再删除")
    node.deleted_at = utcnow()
    audit.log(db, user, "node.delete", "node", node.id, {"name": node.name})
    db.commit()
    return Response(status_code=204)
