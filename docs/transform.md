# Transforming Data

_New in version 2.2.2._

`transform()` modifies a JSON document based on the **initial set of nodes** matching a JSONPath query. It is available as a package-level function, a method of `JSONPathEnvironment`, and a method of compiled `JSONPath` and `CompoundJSONPath` objects.

Unlike a find-and-modify loop, the full match set is computed before anything changes, and every target location is resolved against the document as it was when the operation started. Removing several items from the same array does not suffer from index shifting, and results never depend on the order in which matches were found.

## Removing matches

Pass `delete=True` to remove every matched node. Array items are removed using their indices at the start of the operation.

```python
import jsonpath

data = {
    "items": [
        {"id": 1, "expired": True},
        {"id": 2},
        {"id": 3, "expired": True},
        {"id": 4, "expired": True},
        {"id": 5},
    ]
}

result = jsonpath.transform("$..items[?@.expired]", data, delete=True)
assert result.data == {"items": [{"id": 2}, {"id": 5}]}
```

## Replacing matches with a callback

Pass a function accepting a `JSONPathMatch` and returning a replacement value. All callbacks are invoked - with matches referring to the original, unmodified document - and all targets are validated before any modification is committed. If a callback raises, the document is left unchanged.

```python
result = jsonpath.transform("$.items[*]", data, lambda match: match.obj * 2)
```

Nodes matched by more than one selector - for example by both sides of a union - are modified once only, and the callback is called once per distinct node.

## Conflicting ancestors and descendants

If the initial match set contains a node and one of its ancestors, `transform()` raises `JSONPathTransformConflictError` by default. Choose an explicit `ConflictStrategy` to resolve such overlaps:

```python
from jsonpath import ConflictStrategy

# Keep the ancestor, discard descendants.
jsonpath.transform("$.a | $.a.b", data, delete=True,
                   conflict=ConflictStrategy.PARENT)

# Keep the descendants, discard the ancestor.
jsonpath.transform("$.a | $.a.b", data, delete=True,
                   conflict=ConflictStrategy.CHILD)
```

## Getting an RFC 6902 patch

Pass `return_patch=True` and the result includes a `JSONPatch` containing plain RFC 6902 `remove`/`replace` operations. Applying it to the original document produces exactly the same result.

```python
import copy

original = copy.deepcopy(data)
result = jsonpath.transform("$..items[?@.expired]", data, delete=True,
                            return_patch=True)

assert result.patch is not None
assert result.patch.apply(original) == result.data
```

Target identity is based on normalized path parts, not pointer strings, so object keys that look like array indices, or contain `/` or `~`, can never be confused with each other or with array indices.

## The result object

`transform()` returns a `TransformResult`:

- `data` - the modified document. The input is modified in place, unless the document root was replaced with an object of a different type.
- `matched` - the number of distinct nodes in the initial match set.
- `modified` - the number of nodes actually modified, after conflict resolution.
- `patch` - a `JSONPatch` reproducing the result, or `None` if `return_patch` was not given.
