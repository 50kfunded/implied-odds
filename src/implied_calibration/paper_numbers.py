"""Computed manuscript macros are rendered from retained JSON selectors."""

import ast
from datetime import datetime, timezone
import math
import re

from .inference_study import wilson


def json_path(summary: dict, path: str):
    tokens = re.findall(r'[^.\[\]]+', path.removeprefix('$.'))
    def descend(value, remaining):
        if not remaining:
            return value
        key, *rest = remaining
        if key == '*':
            return [descend(item, rest) for item in value]
        return descend(value[int(key)] if isinstance(value, list) else value[key], rest)
    return descend(summary, tokens)


def expression_value(expression: str, summary: dict):
    expression = expression.replace(' squared', ' ** 2')
    translated = re.sub(r'\$\.[A-Za-z0-9_.\[\]*]+', lambda match: 'path('+repr(match.group())+')', expression)
    node = ast.parse(translated, mode='eval').body
    def visit(part):
        if isinstance(part, ast.Constant) and isinstance(part.value, (int, float, str)):
            return part.value
        if isinstance(part, ast.UnaryOp) and isinstance(part.op, ast.USub):
            return -visit(part.operand)
        if isinstance(part, ast.BinOp):
            a, b = visit(part.left), visit(part.right)
            if isinstance(part.op, ast.Mult):
                return a*b
            if isinstance(part.op, ast.Div):
                return a/b
            if isinstance(part.op, ast.Add):
                return a+b
            if isinstance(part.op, ast.Sub):
                return a-b
            if isinstance(part.op, ast.Pow) and b == 2:
                return a*a
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and len(part.args) == 1 and not part.keywords:
            argument = visit(part.args[0])
            if part.func.id == 'path':
                return json_path(summary, argument)
            if part.func.id == 'min':
                return min(argument)
            if part.func.id == 'max':
                return max(argument)
            if part.func.id == 'len':
                return len(argument)
        raise ValueError('Unsupported numeric macro expression')
    return visit(node)


def computed_value(name: str, source: str, summary: dict) -> str | None:
    if source.startswith('Fixed') or source.startswith('Source bibliography') or source.startswith('Verified'):
        return None
    if name == 'AnnualHoursPlain':
        return str(summary['plan']['annual_hours'])
    if source.startswith('One-year null/alternative Wilson'):
        bound = wilson(0, summary['plan']['simulation']['replications'])
        if bound is None:
            raise ValueError('The simulation replication count must be positive')
        return format(100*bound[1], '.1f')
    if 'one-year null' in source:
        values = [row['ungated_rejection_rate'] for row in summary['inference_simulations']['rows']
                  if row['n'] == 365 and row['population_mean'] == 0]
        return format(100*(min(values) if source.startswith('min') else max(values)), '.0f')
    if 'JSON object key' in source:
        path = source.split('JSON object key')[0]
        json_path(summary, path)
        return path.rsplit('.', 1)[1]
    if '; UTC year' in source:
        value = json_path(summary, source.split(';')[0])
        return str(datetime.fromtimestamp(value/1000, timezone.utc).year)
    if '; source spelling' in source:
        value = json_path(summary, source.split(';')[0])
        mantissa, exponent = format(value, '.0e').split('e')
        return mantissa+'e'+str(int(exponent))
    if 'fixed UTC clock' in source:
        future = summary['plan']['future']
        offset = {'HAROriginTime': 0, 'HARFreezeStartTime': future['freeze_start_before_origin_ms'],
                  'HARFreezeEndTime': future['freeze_end_before_origin_ms']}[name]
        return datetime.fromtimestamp((future['origin_start_ms']-offset)/1000, timezone.utc).strftime('%H:%M')
    if '; UTC date' in source:
        expression = source.split(';')[0]
        value = (summary['plan']['future']['origin_end_ms']+summary['plan']['horizon_hours']*3600000
                 if 'plus horizon_hours' in expression else json_path(summary, expression))
        return datetime.fromtimestamp(value/1000, timezone.utc).strftime('%d %B %Y').lstrip('0')
    if '; format ' in source and ('$.' in source or source.startswith(('min(', 'max('))):
        expression, fmt = source.split('; format ', 1)
        value = expression_value(expression, summary)
        if fmt in ('d', ',d'):
            if not math.isclose(value, round(value), abs_tol=1e-8):
                raise ValueError('Integer macro selector produced a fractional result')
            value = int(round(value))
        return format(value, fmt).replace(',', '{,}')
    if '$.' in source:
        raise ValueError('Unknown computed macro annotation: '+source)
    return None


def render_numbers(source: str, summary: dict, v2_summary: dict | None = None) -> tuple[str, int]:
    count = 0
    pattern = re.compile(r'(?P<comment>% (?P<source>[^\n]+)\n)\\newcommand\{\\(?P<name>\w+)\}\{(?P<value>[^\n]*)\}')
    def replace(match):
        nonlocal count
        note = match['source']
        selected = summary
        if note.startswith('v2.'):
            if v2_summary is None:
                raise ValueError('The manuscript requires the separate v2 summary')
            selected, note = v2_summary, note.removeprefix('v2.')
        value = computed_value(match['name'], note, selected)
        if value is None:
            return match.group()
        count += 1
        return match['comment']+'\\newcommand{\\'+match['name']+'}{'+value+'}'
    return pattern.sub(replace, source), count


def check_numbers(source: str, summary: dict, v2_summary: dict | None = None) -> int:
    generated, count = render_numbers(source, summary, v2_summary)
    if generated != source:
        before = source.splitlines()
        after = generated.splitlines()
        differences = [b+' -> '+a for b, a in zip(before, after) if b != a]
        raise ValueError('Manuscript numeric macros drifted from JSON:\n'+'\n'.join(differences))
    return count
