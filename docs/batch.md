# Batch Queries

When an application runs many JSONPath queries against the same JSON document, and those queries share common prefixes, `jsonpath.compile_many()` can compile them into a single `QueryPlan`. Selection steps that are common to two or more queries, and safe to reuse, are resolved once and shared between those queries.

```python
import jsonpath

plan = jsonpath.compile_many(
    {
        "skus": "$.orders[*].lines[*].sku",
        "quantities": "$.orders[*].lines[*].qty",
        "big_lines": "$.orders[*].lines[?@.qty > 1]",
    }
)

results = plan.findall(data)
print(results["skus"])
```

For every query in the plan, the order of results, the number of duplicate matches and the normalized path of each match are identical to compiling and executing each query individually. Repeated selectors within a single query are never optimized away.

## Errors

`QueryPlan.findall()` and `QueryPlan.finditer()` raise `JSONPathBatchError` if one or more queries fail. The exception's `errors` attribute maps query names to the exception each query raised. Use `QueryPlan.apply()` (or `apply_async()`) to get a `BatchResult` instead, which exposes `results` and `errors` mappings without raising, so one failing query does not prevent sibling queries from completing.

## Pure filter functions

Filter expressions calling custom function extensions are never shared between queries unless every function they call is declared _pure_. Custom functions are treated as potentially side-effecting by default. Set `pure = True` on a `FilterFunction` subclass, or set a `pure` attribute on a plain callable, to declare that repeated evaluation is deterministic and side effect free.

```python
from jsonpath.function_extensions import ExpressionType, FilterFunction


class MyFunc(FilterFunction):
    arg_types = [ExpressionType.VALUE]
    return_type = ExpressionType.LOGICAL
    pure = True  # safe to share evaluation between queries

    def __call__(self, value: object) -> bool:
        return isinstance(value, str) and value.startswith("x")
```

All of the built-in function extensions (`length`, `count`, `match`, `search`, `value`, etc.) are declared pure.

## Async and reuse

`QueryPlan` supports asynchronous data access with `findall_async()`, `finditer_async()` and `apply_async()`, mirroring the single-query [async API](async.md). A compiled plan holds no per-document state, so it can be applied to any number of JSON documents, synchronously or asynchronously, without leaking matches from one application to the next.
