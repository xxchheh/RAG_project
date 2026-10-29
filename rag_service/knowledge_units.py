"""Build retrieval text from reviewed sections, never from isolated headings."""
import re
from .embeddings import split_text


def source_location(blocks):
    values = [b['locator'] for b in blocks]
    matches = [re.fullmatch(r'(.*段落 )(\d+)', v) for v in values]
    if matches and all(matches) and len({m[1] for m in matches}) == 1:
        return matches[0][1]+matches[0][2]+('–'+matches[-1][2] if len(matches) > 1 else '')
    return '；'.join(values)


def prose_units(blocks, unsafe):
    title, group, page = '', [], None
    def flush():
        if not group:
            return []
        text = '\n'.join(b['text'] for b in group).strip()
        raw = '\n'.join(b.get('source_text', b['text']) for b in group)
        return [(group[0]['id'], '\n'.join([title, chunk]).strip(), source_location(group), raw)
                for chunk in split_text(text)]
    units = []
    for block in blocks:
        if block.get('page') != page:
            units.extend(flush()); group = []
            page = block.get('page')
            if page is not None:
                title = block.get('page_title') or block.get('section') or ''
        if block.get('excluded') or block['id'] in unsafe or block.get('heading'):
            units.extend(flush()); group = []
            if block.get('heading') and not block.get('excluded'):
                title = block.get('section') or block['text'].lstrip('# ')
            continue
        group.append(block)
    units.extend(flush())
    return units


def rule_text(rule, descriptions=True):
    parts = [rule['name']]
    if descriptions:
        for field, label in [('phenomena', '机理现象'), ('causes', '机理原因'), ('actions', '解决措施')]:
            if rule.get(field):
                parts.append(label+'：\n'+'\n'.join(rule[field]))
    parts.append(rule['original_text'])
    return '\n'.join(parts)
