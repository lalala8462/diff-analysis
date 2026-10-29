"""有界结构化路径分析；事实不足时保留未知，不执行源码表达式。"""
from copy import deepcopy
from dataclasses import dataclass, field, replace
from .scanner import compatible_source_scope


MAX_STATES = 256
FLOW_RULES = 'behavior-flow-v2'
INTEGER_TYPES = {'byte': 8, 'short': 16, 'char': 16, 'int': 32, 'long': 64}
LITERALS = {'NULL_LITERAL', 'BOOLEAN_LITERAL', 'INT_LITERAL', 'LONG_LITERAL',
            'FLOAT_LITERAL', 'DOUBLE_LITERAL', 'CHAR_LITERAL', 'STRING_LITERAL'}


def key(node):
    if not node:
        return ''
    kind = node['kind']
    if kind == 'IDENTIFIER':
        return node['name']
    if kind == 'MEMBER_SELECT':
        return key(node['receiver']) + '.' + node['name']
    if kind == 'ARRAY_ACCESS':
        return key(node['array']) + '[' + key(node['index']) + ']'
    if kind in LITERALS:
        return repr(node.get('value'))
    if kind == 'METHOD_INVOCATION':
        return key(node['select']) + '(' + ','.join(key(a) for a in node['arguments']) + ')'
    if 'left' in node and 'right' in node:
        return '(' + key(node['left']) + ' ' + kind + ' ' + key(node['right']) + ')'
    if kind == 'LOGICAL_COMPLEMENT':
        return '!' + key(node['value'])
    return kind


@dataclass
class Value:
    constant: object = None
    known: bool = False
    nonnull: bool = False
    origin: str = ''
    dtype: str = ''
    fields: dict = field(default_factory=dict)
    call: str = ''
    upstream_call: str = ''
    failure: bool = False
    resource: tuple = ()
    sources: tuple = ()


def literal(value, dtype=''):
    if dtype == 'char' and isinstance(value, str) and len(value) == 1:
        value = ord(value)
    return Value(value, True, value is not None, dtype=dtype)


def integer_value(value):
    if value.dtype == 'char' and isinstance(value.constant, str) and len(value.constant) == 1:
        return ord(value.constant)
    return value.constant if value.dtype in INTEGER_TYPES and type(value.constant) is int else None


def integer_result(value, dtype):
    """Java 整数按目标位宽截断，char 保留无符号取值。"""
    bits = INTEGER_TYPES[dtype]
    wrapped = value % (1 << bits)
    if dtype != 'char' and wrapped >= 1 << (bits - 1):
        wrapped -= 1 << bits
    return literal(wrapped, dtype)


def typed(value, dtype):
    """声明、赋值和实参转换同样遵循 Java 整数类型。"""
    if not dtype or dtype == value.dtype:
        return value
    if dtype not in INTEGER_TYPES and dtype not in ('float', 'double', 'boolean'):
        return replace(value, dtype=value.dtype or dtype)
    if value.known and dtype in INTEGER_TYPES:
        number = integer_value(value)
        if number is None:
            return replace(value, dtype=dtype, known=False, constant=None)
        return replace(value, dtype=dtype, constant=integer_result(number, dtype).constant)
    if value.known and dtype in ('float', 'double'):
        return replace(value, dtype=dtype, known=False, constant=None)
    return replace(value, dtype=dtype)


@dataclass
class State:
    env: dict = field(default_factory=dict)
    facts: set = field(default_factory=set)
    path: list = field(default_factory=list)
    events: list = field(default_factory=list)
    stop: str = ''
    value: Value = field(default_factory=Value)
    uncertain: bool = False
    checked_calls: set = field(default_factory=set)
    field_names: set = field(default_factory=set)


class Flow:
    def __init__(self, methods, semantics=None, resource_calls=None, max_states=MAX_STATES):
        self.methods = methods
        grouped = {}
        for method in methods.values():
            if method.get('binding_id'):
                grouped.setdefault(method['binding_id'], []).append(method)
        self.bindings = {k: v[0] for k, v in grouped.items() if len(v) == 1}
        self.semantics = semantics or {}
        self.resource_calls = resource_calls or {}
        self.max_states = max_states
        self.gaps = []
        self.steps = 0
        for values in grouped.values():
            if len(values) > 1:
                for method in values:
                    self.gap('DUPLICATE_METHOD_BINDING', method)

    def gap(self, reason, method, node=None):
        item = {'reason': reason, 'symbol': method['symbol'], 'path': method['path'],
                'line': (node or {}).get('line', method['start'])}
        if item not in self.gaps:
            self.gaps.append(item)

    def limit(self, states, method):
        if len(states) > self.max_states:
            self.gap('STATE_BUDGET', method)
            states = states[:self.max_states]
            for state in states:
                state.uncertain = True
        return states

    def event(self, state, method, node, kind, **details):
        state.events.append({'kind': kind, 'symbol': method['symbol'], 'path': method['path'],
                             'line': node.get('line', method['start']),
                             'condition': list(state.path), 'unknown': state.uncertain, **details})

    def run(self, method, arguments=None, depth=0, receiver=None, argument_facts=()):
        behavior = method.get('behavior') or {}
        if not behavior.get('body'):
            self.gap('METHOD_BODY_UNAVAILABLE', method)
            return [State(uncertain=True)]
        state = State(facts=set(argument_facts))
        for i, param in enumerate(behavior['parameters']):
            state.env[param['name']] = (typed(deepcopy(arguments[i]), param['type']) if arguments is not None and i < len(arguments)
                                       else Value(origin=param['name'], dtype=param['type']))
        state.env['this'] = receiver or Value(nonnull=True, origin='this')
        state.field_names = {f['name'] for f in behavior.get('fields', [])} - {p['name'] for p in behavior['parameters']}
        result = self.statement(behavior['body'], [state], method, depth)
        for s in result:
            if s.stop == 'throw':
                self.event(s, method, behavior['body'], 'THROW', exception=s.value.dtype)
            elif not s.stop:
                s.stop, s.value = 'return', Value()
        return result

    def require(self, state, method, node, value, requirement, expression):
        if state.stop:
            return
        origin = value.origin or expression
        guaranteed = ((requirement == 'NONNULL' and (value.nonnull or (value.known and value.constant is not None)))
                      or (requirement == 'NONZERO' and value.known and value.constant not in (None, 0))
                      or (requirement, origin) in state.facts)
        if not guaranteed:
            self.event(state, method, node, 'REQUIREMENT', requirement=requirement,
                       expression=expression, origin=origin, call=value.call,
                       related_calls=[value.upstream_call] if value.upstream_call else [],
                       known=value.known, value=value.constant, resource=list(value.resource))

    def refine(self, state, node, truth):
        if not node:
            return state
        kind = node['kind']
        if kind == 'LOGICAL_COMPLEMENT':
            return self.refine(state, node['value'], not truth)
        if kind in ('EQUAL_TO', 'NOT_EQUAL_TO', 'GREATER_THAN', 'GREATER_THAN_EQUAL',
                    'LESS_THAN', 'LESS_THAN_EQUAL'):
            left, right = node['left'], node['right']
            if left['kind'] in LITERALS and right['kind'] not in LITERALS:
                left, right = right, left
                kind = {'GREATER_THAN': 'LESS_THAN', 'LESS_THAN': 'GREATER_THAN',
                        'GREATER_THAN_EQUAL': 'LESS_THAN_EQUAL', 'LESS_THAN_EQUAL': 'GREATER_THAN_EQUAL'}.get(kind, kind)
            name = key(left)
            current = state.env.get(name, Value(origin=name))
            if right['kind'] in LITERALS:
                value = right.get('value')
                if right['kind'] == 'CHAR_LITERAL' and isinstance(value, str):
                    value = ord(value)
                equal = truth == (kind == 'EQUAL_TO') if kind in ('EQUAL_TO', 'NOT_EQUAL_TO') else None
                if equal is not None:
                    if current.known and (current.constant == value) != equal:
                        return None
                    if equal:
                        new = replace(current, constant=value, known=True, nonnull=value is not None)
                        self.alias_refine(state, current, new, name)
                    elif value is None:
                        self.alias_refine(state, current, replace(current, nonnull=True), name)
                    elif value == 0:
                        state.facts.add(('NONZERO', current.origin or name))
                elif value == 0 and ((truth and kind in ('GREATER_THAN', 'GREATER_THAN_EQUAL'))
                                    or (not truth and kind in ('LESS_THAN', 'LESS_THAN_EQUAL'))):
                    if (truth and kind == 'GREATER_THAN') or (not truth and kind == 'LESS_THAN_EQUAL'):
                        state.facts.add(('NONZERO', current.origin or name))
                    state.facts.add(('NONNEGATIVE', current.origin or name))
                if type(value) is int and kind not in ('EQUAL_TO', 'NOT_EQUAL_TO'):
                    origin = current.origin or name
                    lower = (value + (kind == 'GREATER_THAN') if truth and kind in ('GREATER_THAN', 'GREATER_THAN_EQUAL') else
                             value + (kind == 'LESS_THAN_EQUAL') if not truth and kind in ('LESS_THAN', 'LESS_THAN_EQUAL') else None)
                    upper = (value - (kind == 'LESS_THAN') if truth and kind in ('LESS_THAN', 'LESS_THAN_EQUAL') else
                             value - (kind == 'GREATER_THAN_EQUAL') if not truth and kind in ('GREATER_THAN', 'GREATER_THAN_EQUAL') else None)
                    if lower is not None:
                        state.facts.add(('LOWER', origin, lower))
                    if upper is not None:
                        state.facts.add(('UPPER', origin, upper))
                    lowers = [f[2] for f in state.facts if f[0] == 'LOWER' and f[1] == origin]
                    uppers = [f[2] for f in state.facts if f[0] == 'UPPER' and f[1] == origin]
                    if lowers and uppers and max(lowers) > min(uppers):
                        return None
                    if lowers and max(lowers) >= 0:
                        state.facts.add(('NONNEGATIVE', origin))
                    if (lowers and max(lowers) > 0) or (uppers and min(uppers) < 0):
                        state.facts.add(('NONZERO', origin))
            elif right['kind'] == 'MEMBER_SELECT' and right['name'] == 'length':
                if (kind == 'LESS_THAN' and truth) or (kind == 'GREATER_THAN_EQUAL' and not truth):
                    array = state.env.get(key(right['receiver']), Value(origin=key(right['receiver'])))
                    state.facts.add(('BELOW_LENGTH', current.origin or name, array.origin))
        elif kind in ('IDENTIFIER', 'MEMBER_SELECT'):
            name = key(node)
            current = state.env.get(name, Value(origin=name))
            if current.known and isinstance(current.constant, bool) and current.constant != truth:
                return None
            self.alias_refine(state, current, replace(current, known=True, constant=truth), name)
        return state

    @staticmethod
    def alias_refine(state, current, new, name):
        for alias, value in list(state.env.items()):
            if value.origin and value.origin == current.origin:
                state.env[alias] = new
        state.env[name] = new

    def branches(self, node, state, method, depth):
        # 短路条件分别求值，右侧只在对应路径执行。
        if node['kind'] == 'CONDITIONAL_AND':
            yes, no = self.branches(node['left'], state, method, depth)
            all_yes = []
            for s in yes:
                y, n = self.branches(node['right'], s, method, depth)
                all_yes += y
                no += n
            return self.limit(all_yes, method), self.limit(no, method)
        if node['kind'] == 'CONDITIONAL_OR':
            yes, no = self.branches(node['left'], state, method, depth)
            all_no = []
            for s in no:
                y, n = self.branches(node['right'], s, method, depth)
                yes += y
                all_no += n
            return self.limit(yes, method), self.limit(all_no, method)
        yes, no = [], []
        for s, value in self.expression(node, state, method, depth):
            if s.stop:
                no.append(s)
                continue
            if (not value.known and node['kind'] in ('LESS_THAN', 'LESS_THAN_EQUAL', 'GREATER_THAN', 'GREATER_THAN_EQUAL')
                    and node.get('right', {}).get('kind') not in LITERALS
                    and not (node.get('right', {}).get('kind') == 'MEMBER_SELECT' and node['right']['name'] == 'length')):
                self.gap('CONDITION_CONSTRAINT_UNKNOWN', method, node)
                s.uncertain = True
            for truth, bucket in ((True, yes), (False, no)):
                if value.known and isinstance(value.constant, bool) and value.constant != truth:
                    continue
                branch = self.refine(deepcopy(s), node, truth)
                if branch is not None:
                    if value.call:
                        branch.checked_calls.add(value.call)
                    if value.upstream_call:
                        branch.checked_calls.add(value.upstream_call)
                    branch.path.append({'expression': key(node), 'truth': truth, 'line': node['line']})
                    bucket.append(branch)
        return yes, no

    def expression(self, node, state, method, depth):
        if node is None:
            return [(state, Value())]
        if state.stop:
            return [(state, state.value)]
        kind = node['kind']
        if node.get('constant_known'):
            return [(state, literal(node['constant'], node.get('java_type', '')))]
        if kind in LITERALS:
            dtype = node.get('java_type') or {'INT_LITERAL': 'int', 'LONG_LITERAL': 'long',
                'CHAR_LITERAL': 'char', 'BOOLEAN_LITERAL': 'boolean', 'FLOAT_LITERAL': 'float',
                'DOUBLE_LITERAL': 'double', 'STRING_LITERAL': 'java.lang.String'}.get(kind, '')
            return [(state, replace(literal(node.get('value'), dtype), sources=((method['path'], node['line']),)))]
        if kind == 'IDENTIFIER':
            if node.get('binding'):
                return [(state, literal(node['binding']))]
            receiver = state.env.get('this', Value())
            resource = receiver.resource + (node['name'],) if node.get('field') and receiver.resource else ()
            fallback = receiver.fields.get(node['name'], Value(origin=node['name'], call=receiver.call, resource=resource))
            if node.get('field') and receiver.call and not fallback.call:
                fallback = replace(fallback, call=receiver.call, upstream_call=receiver.upstream_call)
            return [(state, state.env.get(node['name'], fallback))]
        if kind == 'MEMBER_SELECT':
            if node.get('binding'):
                return [(state, literal(node['binding']))]
            result = []
            for s, receiver in self.expression(node['receiver'], state, method, depth):
                if s.stop:
                    result.append((s, receiver))
                    continue
                self.require(s, method, node, receiver, 'NONNULL', key(node['receiver']))
                name = key(node)
                value = s.env.get(name, receiver.fields.get(node['name'],
                    Value(origin=receiver.origin + '.' + node['name'], call=receiver.call,
                          resource=receiver.resource + (node['name'],) if receiver.resource else ())))
                if receiver.call and not value.call:
                    value = replace(value, call=receiver.call, upstream_call=receiver.upstream_call)
                result.append((s, value))
            return result
        if kind in ('CONDITIONAL_AND', 'CONDITIONAL_OR'):
            yes, no = self.branches(node, state, method, depth)
            return [(s, literal(True)) for s in yes] + [(s, literal(False)) for s in no]
        if kind == 'CONDITIONAL_EXPRESSION':
            yes, no = self.branches(node['condition'], state, method, depth)
            return [(s2, v) for states, child in ((yes, node['then']), (no, node['else']))
                    for s in states for s2, v in self.expression(child, s, method, depth)]
        if kind in ('ASSIGNMENT',):
            result = []
            target = node['target']
            for s, value in self.expression(node['value'], state, method, depth):
                if s.stop:
                    result.append((s, value))
                    continue
                value = typed(value, target.get('java_type', ''))
                if target['kind'] == 'MEMBER_SELECT':
                    for s2, receiver in self.expression(target['receiver'], s, method, depth):
                        self.require(s2, method, node, receiver, 'NONNULL', key(target['receiver']))
                        receiver.fields[target['name']] = value
                name = key(target)
                old = s.env.get(name)
                s.env[name] = value
                if target['kind'] == 'IDENTIFIER' and target.get('field'):
                    s.env['this'].fields[target['name']] = value
                if target['kind'] == 'MEMBER_SELECT' or target.get('field'):
                    self.event(s, method, node, 'FIELD_WRITE', expression=name)
                if old:
                    s.facts = {f for f in s.facts if old.origin not in f[1:]}
                result.append((s, value))
            return result
        if kind == 'TYPE_CAST':
            result = []
            for s, v in self.expression(node['value'], state, method, depth):
                dtype = node.get('java_type', node['type'])
                if dtype in INTEGER_TYPES and v.known:
                    number = integer_value(v)
                    if number is None:
                        self.gap('NUMERIC_CONVERSION_UNKNOWN', method, node)
                        s.uncertain = True
                        v = replace(v, known=False, constant=None, dtype=dtype)
                    else:
                        v = replace(integer_result(number, dtype), call=v.call, upstream_call=v.upstream_call)
                else:
                    if dtype in ('float', 'double') and v.known:
                        self.gap('NUMERIC_CONVERSION_UNKNOWN', method, node)
                        s.uncertain = True
                        v = replace(v, known=False, constant=None)
                    widening = {'byte': {'short', 'int', 'long'}, 'short': {'int', 'long'},
                                'char': {'int', 'long'}, 'int': {'long'}}
                    same_value = dtype == v.dtype or dtype in widening.get(v.dtype, set())
                    v = replace(v, dtype=dtype, origin=v.origin if same_value else '')
                result.append((s, v))
            return result
        if kind in ('NEW_CLASS', 'NEW_ARRAY'):
            result = [(state, [])]
            args = (node.get('arguments', []) if kind == 'NEW_CLASS' else
                    node.get('items') if node.get('items') is not None else node.get('dimensions', []))
            for arg in args:
                result = [(s2, vals + [v]) for s, vals in result
                          for s2, v in self.expression(arg, s, method, depth)]
            output = []
            constructor = self.bindings.get(node.get('binding'))
            if constructor and not compatible_source_scope(method, constructor):
                constructor = None
            for s, arguments in result:
                if s.stop:
                    output.append((s, s.value))
                    continue
                value = Value(nonnull=True, dtype=node.get('binding', '').partition('#')[0] or node.get('type', 'array'),
                              origin='new@' + str(node['offset']))
                if constructor:
                    for f in constructor['behavior'].get('fields', []):
                        initializer = f.get('value')
                        if initializer and initializer['kind'] in LITERALS:
                            value.fields[f['name']] = replace(literal(initializer.get('value')),
                                                              sources=((constructor['path'], initializer['line']),))
                        elif not initializer:
                            default = False if f['type'] == 'boolean' else 0 if f['type'] in ('int', 'long', 'short', 'byte', 'double', 'float', 'char') else None
                            value.fields[f['name']] = replace(literal(default), sources=((constructor['path'], f['line']),))
                if kind == 'NEW_ARRAY':
                    if node.get('items') is not None:
                        value.fields['length'] = literal(len(node['items']), 'int')
                    elif arguments and arguments[0].known:
                        value.fields['length'] = arguments[0]
                if constructor and depth < 1:
                    for child in self.run(constructor, arguments, depth + 1, value,
                                          self.argument_facts(s, arguments)):
                        branch = deepcopy(s)
                        branch.events += [{**e, 'condition': s.path + e.get('condition', [])} for e in child.events]
                        branch.path += child.path
                        branch.uncertain |= child.uncertain
                        if child.stop == 'throw':
                            branch.stop, branch.value = 'throw', child.value
                            output.append((branch, child.value))
                            continue
                        output.append((branch, replace(value, fields={**value.fields, **child.env.get('this', Value()).fields})))
                else:
                    if constructor:
                        self.gap('CALL_DEPTH_LIMIT', method, node)
                        s.uncertain = True
                        params = constructor['behavior']['parameters']
                        values = {p['name']: a for p, a in zip(params, arguments)}
                        body = constructor['behavior'].get('body') or {}
                        for item in body.get('items', []):
                            assignment = item.get('value') or {}
                            target_node = assignment.get('target') or {}
                            rhs = assignment.get('value') or {}
                            if (assignment.get('kind') == 'ASSIGNMENT' and
                                    ((target_node.get('kind') == 'MEMBER_SELECT' and key(target_node.get('receiver')) == 'this') or
                                     (target_node.get('kind') == 'IDENTIFIER' and target_node.get('field')))):
                                v = values.get(rhs.get('name')) if rhs.get('kind') == 'IDENTIFIER' else literal(rhs.get('value')) if rhs.get('kind') in LITERALS else None
                                if v is not None:
                                    value.fields[target_node['name']] = v
                    output.append((s, value))
            return output
        if kind == 'ARRAY_ACCESS':
            result = []
            for s, array in self.expression(node['array'], state, method, depth):
                self.require(s, method, node, array, 'NONNULL', key(node['array']))
                for s2, index in self.expression(node['index'], s, method, depth):
                    length = array.fields.get('length', Value())
                    safe = ((('NONNEGATIVE', index.origin) in s2.facts or
                             (index.known and isinstance(index.constant, int) and index.constant >= 0)) and
                            (('BELOW_LENGTH', index.origin, array.origin) in s2.facts or
                             (index.known and length.known and isinstance(index.constant, int) and
                              isinstance(length.constant, int) and index.constant < length.constant)))
                    if not safe:
                        self.event(s2, method, node, 'REQUIREMENT', requirement='ARRAY_BOUNDS',
                                   expression=key(node), origin=index.origin, call=index.call,
                                   known=index.known, value=index.constant, resource=[])
                    result.append((s2, Value(origin=key(node), call=array.call, resource=array.resource)))
            return result
        if kind == 'METHOD_INVOCATION':
            return self.call(node, state, method, depth)
        if 'left' in node and 'right' in node:
            result = []
            for s, left in self.expression(node['left'], state, method, depth):
                for s2, right in self.expression(node['right'], s, method, depth):
                    if kind in ('DIVIDE', 'REMAINDER'):
                        if node.get('type') in ('int', 'long', 'short', 'byte', 'char'):
                            self.require(s2, method, node, right, 'NONZERO', key(node['right']))
                        else:
                            self.gap('DIVISION_TYPE_UNKNOWN_OR_FLOAT', method, node)
                    value = Value(origin=key(node))
                    if kind in ('PLUS', 'MINUS', 'MULTIPLY') and node.get('java_type') not in (
                            *INTEGER_TYPES, 'java.lang.String'):
                        self.gap('NUMERIC_OPERATION_UNKNOWN', method, node)
                        s2.uncertain = True
                    for observed, constant in ((left, right), (right, left)):
                        if observed.resource and constant.known and kind in ('EQUAL_TO', 'NOT_EQUAL_TO'):
                            self.event(s2, method, node, 'READ_TEST', resource=list(observed.resource),
                                       accepted=[constant.constant], expression=key(node), equal=kind == 'EQUAL_TO')
                    if left.known and right.known:
                        a, b = left.constant, right.constant
                        try:
                            arithmetic = kind in ('PLUS', 'MINUS', 'MULTIPLY')
                            x, y = integer_value(left), integer_value(right)
                            if x is not None and y is not None:
                                a, b = x, y
                            op = {'EQUAL_TO': lambda: a == b, 'NOT_EQUAL_TO': lambda: a != b,
                                  'LESS_THAN': lambda: a < b, 'LESS_THAN_EQUAL': lambda: a <= b,
                                  'GREATER_THAN': lambda: a > b, 'GREATER_THAN_EQUAL': lambda: a >= b,
                                  'PLUS': lambda: a + b, 'MINUS': lambda: a - b,
                                  'MULTIPLY': lambda: a * b}
                            if arithmetic and x is not None and y is not None:
                                dtype = 'long' if 'long' in (left.dtype, right.dtype) else 'int'
                                number = x + y if kind == 'PLUS' else x - y if kind == 'MINUS' else x * y
                                value = integer_result(number, dtype)
                            elif arithmetic and not (kind == 'PLUS' and left.dtype == right.dtype == 'java.lang.String'):
                                self.gap('NUMERIC_OPERATION_UNKNOWN', method, node)
                                s2.uncertain = True
                            elif not arithmetic and (type(a) in (int, float) or type(b) in (int, float)) and (x is None or y is None):
                                self.gap('NUMERIC_COMPARISON_UNKNOWN', method, node)
                                s2.uncertain = True
                            elif kind in op:
                                value = literal(op[kind]())
                        except (TypeError, ValueError, OverflowError):
                            pass
                    value = replace(value, call=left.call or right.call,
                                    upstream_call=left.upstream_call or right.upstream_call)
                    result.append((s2, value))
            return result
        if kind in ('LOGICAL_COMPLEMENT', 'UNARY_MINUS', 'UNARY_PLUS'):
            result = []
            for s, value in self.expression(node['value'], state, method, depth):
                if value.known:
                    try:
                        number = integer_value(value)
                        if kind == 'LOGICAL_COMPLEMENT' and type(value.constant) is bool:
                            computed = literal(not value.constant, 'boolean')
                        elif kind != 'LOGICAL_COMPLEMENT' and number is not None:
                            computed = integer_result(-number if kind == 'UNARY_MINUS' else number,
                                                      'long' if value.dtype == 'long' else 'int')
                        else:
                            self.gap('NUMERIC_OPERATION_UNKNOWN', method, node)
                            s.uncertain = True
                            computed = Value()
                        value = replace(computed, call=value.call, upstream_call=value.upstream_call)
                    except TypeError:
                        value = Value()
                result.append((s, value))
            return result
        self.gap(node.get('reason', 'UNSUPPORTED_EXPRESSION_' + kind), method, node)
        state.uncertain = True
        state.facts.clear()
        state.env = {k: replace(v, known=False, nonnull=False, fields={}) for k, v in state.env.items() if '.' not in k}
        return [(state, Value())]

    @staticmethod
    def argument_facts(state, arguments):
        """仅传递全部来源均为实参的约束，避免带入调用方无关局部变量。"""
        origins = {v.origin for v in arguments if v.origin}
        result = set()
        for fact in state.facts:
            names = fact[1:3] if fact[0] == 'BELOW_LENGTH' else fact[1:2]
            if names and all(name in origins for name in names):
                result.add(fact)
        return result

    def call(self, node, state, method, depth):
        binding = node.get('binding', '')
        select = node['select']
        receiver = select.get('receiver')
        # 已归属的静态调用不要求类名称非空。
        target = self.bindings.get(binding)
        if target and not compatible_source_scope(method, target):
            target = None
        static = target and target.get('behavior', {}).get('static')
        states = [(state, [], state.env.get('this'))]
        if receiver and not static:
            # 包名/类型限定的静态方法由 javac 的修饰符事实标记。
            if not node.get('static', False):
                states = []
                for s, value in self.expression(receiver, state, method, depth):
                    self.require(s, method, node, value, 'NONNULL', key(receiver))
                    states.append((s, [], value))
        for arg in node['arguments']:
            states = [(s2, args + [v], receiver_value) for s, args, receiver_value in states
                      for s2, v in self.expression(arg, s, method, depth)]
            if len(states) > self.max_states:
                self.gap('STATE_BUDGET', method, node)
                states = states[:self.max_states]
                for s, _, _ in states:
                    s.uncertain = True
        output = []
        call_id = binding or key(select)
        for s, arguments, receiver_value in states[:self.max_states]:
            if s.stop:
                output.append((s, s.value))
                continue
            self.event(s, method, node, 'CALL', binding=binding, call=call_id,
                       arguments=[self.value_fact(a) for a in arguments])
            adapter = self.resource_calls.get((method['path'], node['offset']))
            rules = [r for r in self.semantics.get('effects', []) if r['method'] == binding and binding]
            if adapter and adapter['role'] == 'WRITE':
                self.event(s, method, node, 'EFFECT', operation=adapter['operation'], resource=adapter['resource'])
                for fact in adapter.get('sql_facts', []):
                    if fact['role'] != 'WRITE':
                        continue
                    value = literal(fact['value']) if fact['known'] else Value()
                    if not fact['known'] and target:
                        names = [p['name'] for p in target.get('behavior', {}).get('parameters', [])]
                        tokens = (fact.get('value') or '').split('.')
                        if tokens[0] in names and names.index(tokens[0]) < len(arguments):
                            value = arguments[names.index(tokens[0])]
                            for token in tokens[1:]:
                                value = value.fields.get(token, Value())
                    self.event(s, method, node, 'SQL_WRITE_VALUE', resource=adapter['resource'],
                               field=fact['field'], value=self.value_fact(value), declaration=fact)
                index = adapter.get('value_argument')
                if index is not None and index < len(arguments):
                    self.event(s, method, node, 'WRITE_VALUE', resource=adapter['resource'],
                               value=self.value_fact(arguments[index]),
                               fields={k: self.value_fact(v) for k, v in arguments[index].fields.items()})
            for rule in rules:
                index = rule.get('resource_argument')
                if index is not None and index >= len(arguments):
                    self.gap('SEMANTICS_ARGUMENT_OUT_OF_RANGE', method, node)
                    continue
                resource = arguments[index].constant if isinstance(index, int) and index < len(arguments) and arguments[index].known else None
                self.event(s, method, node, 'EFFECT', operation=rule['operation'], resource=resource,
                           binding=binding)
            if target and target.get('behavior', {}).get('body') and depth < 1 and not adapter:
                children = self.run(target, arguments, depth + 1, receiver_value,
                                    self.argument_facts(s, arguments))
                for child in children:
                    branch = deepcopy(s)
                    branch.events += [{**e, 'condition': s.path + e.get('condition', [])} for e in child.events]
                    branch.path += child.path
                    branch.uncertain |= child.uncertain
                    if any(e['kind'] == 'FIELD_WRITE' for e in child.events):
                        branch.env = {k: v for k, v in branch.env.items() if '.' not in k and k not in branch.field_names}
                        if receiver_value and receiver_value.origin == branch.env.get('this', Value()).origin:
                            branch.env['this'] = child.env.get('this', branch.env['this'])
                        branch.facts.clear()
                    if child.stop == 'throw':
                        branch.stop, branch.value = 'throw', child.value
                    value = replace(child.value, call=call_id,
                                    upstream_call=receiver_value.call if receiver_value and receiver_value.call else child.value.upstream_call)
                    value = self.apply_result(binding, value, method, node)
                    if branch.stop == 'throw':
                        branch.value = value
                    self.event(branch, method, node, 'OUTCOME', call=call_id, **self.value_fact(value))
                    output.append((branch, value))
            else:
                if target and not adapter:
                    self.gap('CALL_DEPTH_LIMIT' if depth >= 1 else 'METHOD_BODY_UNAVAILABLE', method, node)
                    s.uncertain = True
                elif not adapter and not rules:
                    if binding != 'java.lang.Object#<init>()':
                        self.gap('CALL_SUMMARY_UNKNOWN', method, node)
                        s.uncertain = True
                # 未知调用可能修改对象字段，撤销字段和数组内容保证。
                s.facts = {f for f in s.facts if all('.' not in str(x) and '[' not in str(x) for x in f[1:])}
                s.env = {k: replace(v, fields={}) for k, v in s.env.items() if '.' not in k and k not in s.field_names}
                value = self.apply_result(binding, Value(origin=key(node), call=call_id, dtype=node.get('return_type', '')), method, node)
                if adapter and adapter['role'] == 'READ':
                    value.resource = tuple(adapter['resource_identity'])
                    projections = [f for f in adapter.get('sql_facts', []) if f['role'] == 'READ']
                    if projections:
                        value.fields = {f.get('output', f['field']): Value(resource=value.resource + (f['field'],), call=call_id,
                                                                          origin=f['field']) for f in projections}
                        if len(projections) == 1 and node.get('return_type') in ('int', 'long', 'boolean', 'java.lang.String', 'java.lang.Integer', 'java.lang.Long'):
                            value.resource += (projections[0]['field'],)
                mapped = next((r for r in self.semantics.get('results', []) if
                               (binding and r.get('method') == binding) or
                               (r.get('type') and r['type'] == value.dtype)), None)
                if mapped:
                    self.gap('MAPPED_RESULT_POSSIBILITIES_NOT_EXECUTION', method, node)
                    values = mapped['failure_values'] + mapped.get('success_values', [])
                    for possible in values:
                        branch = deepcopy(s)
                        branch.uncertain = True
                        if mapped.get('member'):
                            outcome = replace(value, fields={mapped['member']: literal(possible)})
                        else:
                            outcome = replace(value, constant=possible, known=True, nonnull=possible is not None)
                        outcome = self.apply_result(binding, outcome, method, node)
                        self.event(branch, method, node, 'OUTCOME', call=call_id, **self.value_fact(outcome))
                        output.append((branch, outcome))
                else:
                    self.event(s, method, node, 'OUTCOME', call=call_id, **self.value_fact(value))
                    output.append((s, value))
        return output

    def apply_result(self, binding, value, method, node):
        classifications = set()
        for rule in self.semantics.get('results', []):
            if not ((binding and rule.get('method') == binding) or
                    (rule.get('type') and value.dtype == rule['type'])):
                continue
            observed = value.fields.get(rule.get('member'), Value()) if rule.get('member') else value
            if rule.get('member') and rule['member'] not in value.fields:
                self.gap('RESULT_MEMBER_UNKNOWN', method, node)
            if observed.known:
                matches = lambda values: any(type(v) is type(observed.constant) and v == observed.constant for v in values)
                if matches(rule['failure_values']):
                    classifications.add(True)
                    value = replace(value, failure=True)
                elif matches(rule.get('success_values', [])):
                    classifications.add(False)
                elif rule.get('success_values') and not matches(rule['success_values']):
                    self.gap('RESULT_MAPPING_CONFLICT', method, node)
        if len(classifications) > 1:
            self.gap('RESULT_MAPPING_CONFLICT', method, node)
            value = replace(value, failure=False)
        return value

    @staticmethod
    def value_fact(value):
        return {'known': value.known, 'value': value.constant, 'origin': value.origin,
                'nonnull': value.nonnull, 'type': value.dtype, 'failure': value.failure,
                'resource': list(value.resource), 'sources': [list(s) for s in value.sources]}

    def statement(self, node, states, method, depth):
        if not node:
            return states
        self.steps += 1
        if self.steps > 20000:
            self.gap('FLOW_STEP_BUDGET', method, node)
            for s in states:
                s.uncertain = True
            return states
        active = [s for s in states if not s.stop]
        stopped = [s for s in states if s.stop]
        kind = node['kind']
        output = []
        if kind == 'BLOCK':
            output = active
            for child in node['items']:
                output = self.statement(child, output, method, depth)
        elif kind in ('VARIABLE', 'EXPRESSION_STATEMENT', 'RETURN', 'THROW'):
            for s in active:
                for s2, value in self.expression(node.get('value'), s, method, depth):
                    if not s2.stop:
                        if kind == 'VARIABLE':
                            s2.env[node['name']] = typed(value, node['type'])
                            s2.field_names.discard(node['name'])
                        elif kind in ('RETURN', 'THROW'):
                            if kind == 'RETURN':
                                value = typed(value, method.get('behavior', {}).get('return_type', ''))
                            s2.stop, s2.value = kind.lower(), value
                    output.append(s2)
        elif kind == 'IF':
            for s in active:
                yes, no = self.branches(node['condition'], s, method, depth)
                output += self.statement(node['then'], yes, method, depth)
                output += self.statement(node.get('else'), no, method, depth)
        elif kind == 'SWITCH':
            for s in active:
                for s2, value in self.expression(node['value'], s, method, depth):
                    cases = node['cases']
                    def labels(case):
                        label = case.get('value') or {}
                        raw = label.get('binding') or label.get('value')
                        return ord(raw) if label.get('kind') == 'CHAR_LITERAL' and isinstance(raw, str) else raw
                    known_labels = [labels(c) for c in cases if c.get('value') and
                                    (c['value']['kind'] in LITERALS or c['value'].get('binding'))]
                    fallback = False
                    for c in cases:
                        if c.get('value') is None and c['items']:
                            last = c['items'][-1]
                            returned = last.get('value') or {}
                            fallback |= last['kind'] == 'RETURN' and returned.get('kind') in LITERALS and returned.get('value') is not None
                    if value.resource:
                        self.event(s2, method, node, 'READ_DOMAIN', resource=list(value.resource),
                                   accepted=known_labels, fallback=fallback)
                    if value.call and value.known and value.constant not in known_labels and not fallback:
                        self.event(s2, method, node, 'RESULT_DOMAIN', call=value.call,
                                   accepted=known_labels, value=value.constant,
                                   assumption='调用方有限分支需要覆盖新的返回取值')
                    matched = False
                    for case in cases:
                        label = case.get('value')
                        if value.known and label and (label['kind'] in LITERALS or label.get('binding')) and value.constant != labels(case):
                            continue
                        if value.known and label is None and value.constant in known_labels:
                            continue
                        matched = True
                        branch = deepcopy(s2)
                        if label:
                            branch.env[key(node['value'])] = literal(labels(case)) if label['kind'] in LITERALS or label.get('binding') else Value()
                        branch.path.append({'switch': key(node['value']), 'case': key(label), 'line': case['line']})
                        branch_states = self.statement({'kind': 'BLOCK', 'items': case['items']}, [branch], method, depth)
                        # 有落空执行的 case 暂不推断为完整路径。
                        for b in branch_states:
                            if not b.stop:
                                b.uncertain = True
                                self.gap('SWITCH_FALLTHROUGH_UNKNOWN', method, case)
                            if b.stop == 'break':
                                b.stop = ''
                        output += branch_states
                    if not matched or (not value.known and not any(c.get('value') is None for c in cases)):
                        output.append(deepcopy(s2))
        elif kind == 'TRY':
            if node.get('resources'):
                self.gap('TRY_RESOURCES_UNKNOWN', method, node)
                for s in active:
                    s.uncertain = True
            body = self.statement(node['body'], active, method, depth)
            if node['catches'] and any(e['kind'] in ('CALL', 'REQUIREMENT') for s in body for e in s.events):
                self.gap('IMPLICIT_EXCEPTION_PATHS_UNMODELED', method, node)
                for s in body:
                    s.uncertain = True
                    for event in s.events:
                        event['unknown'] = True
            for s in body:
                handled = False
                if s.stop == 'throw':
                    for catch in node['catches']:
                        types = [t.strip() for t in catch['type'].split('|')]
                        if s.value.dtype in types:
                            s.stop = ''
                            s.env[catch['name']] = s.value
                            output += self.statement(catch['body'], [s], method, depth)
                            handled = True
                            break
                    if not handled and node['catches']:
                        self.gap('EXCEPTION_MATCH_UNKNOWN', method, node)
                        s.uncertain = True
                if not handled:
                    output.append(s)
            if node.get('finally'):
                final = []
                for s in output:
                    stop, value = s.stop, s.value
                    s.stop = ''
                    for f in self.statement(node['finally'], [s], method, depth):
                        if not f.stop:
                            f.stop, f.value = stop, value
                        final.append(f)
                output = final
        elif kind == 'BREAK':
            for s in active:
                s.stop = 'break'
            output = active
        elif kind == 'EMPTY_STATEMENT':
            output = active
        else:
            self.gap(node.get('reason', 'UNSUPPORTED_STATEMENT_' + kind), method, node)
            for s in active:
                s.uncertain = True
                s.facts.clear()
                s.env = {k: replace(v, known=False, nonnull=False, fields={}) for k, v in s.env.items() if '.' not in k}
            output = active
        return self.limit(stopped + output, method)
