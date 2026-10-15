"""Reading a post's comments as a reply tree.

Both the community thread and the personal thread show the same tree; only the URLs that
the template builds from it differ, so the pruning rules live here once.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Comment, User


def comment_tree(
    database: Session, post_id: UUID, hidden_author_ids: set[UUID]
) -> list[dict]:
    """Root nodes of one post's replies, newest last, with hidden authors pruned.

    A hidden author takes their whole branch with them: a reply whose parent is hidden is
    hidden too, because the conversation it answers is no longer visible.
    """
    rows = database.execute(
        select(Comment, User)
        .join(User, User.id == Comment.author_id)
        .where(Comment.post_id == post_id)
        .order_by(Comment.created_at)
    ).all()
    nodes = {}
    hidden_comment_ids = set()
    for comment, author in rows:
        if author.id in hidden_author_ids or (
            comment.parent_id is not None and comment.parent_id in hidden_comment_ids
        ):
            hidden_comment_ids.add(comment.id)
            continue
        nodes[comment.id] = {
            "comment": comment,
            "author": author,
            "children": [],
        }
    roots = []
    for node in nodes.values():
        parent_id = node["comment"].parent_id
        if parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        elif parent_id is None:
            roots.append(node)
    return roots
