"""Reproducible static inventory; no imports from audited code, no host access."""
import ast
import argparse
import collections
import csv
import hashlib
import json
import pathlib
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo-root',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[3])
parser.add_argument('--source-ref',default='9d3168afb35edf5ad06e6b5e419801493fe04572')
parser.add_argument('--out',type=pathlib.Path,required=True)
args=parser.parse_args()
ROOT=args.repo_root.resolve();OUT=args.out.resolve();OUT.mkdir(parents=True,exist_ok=True)
ref=subprocess.check_output(['git','rev-parse',args.source_ref+'^{commit}'],cwd=ROOT,text=True).strip()
paths = subprocess.check_output(['git', 'ls-tree','-r','--name-only','-z',ref], cwd=ROOT).decode().split('\0')[:-1]
records = []
modules = {}
for name in paths:
    p = ROOT / name
    raw = subprocess.check_output(['git','show',ref+':'+name],cwd=ROOT)
    record = dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), lines=len(raw.splitlines()), category=name.split('/')[0])
    if p.suffix == '.py':
        try:
            tree = ast.parse(raw, filename=name)
            record['ast'] = 'PARSED'
            record['purpose'] = (ast.get_docstring(tree) or '').split('\n')[0]
            record['definitions'] = [n.name for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            record['imports'] = []
            record['calls'] = []
            record['dynamic_imports'] = []
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    record['imports'].extend(x.name for x in n.names)
                if isinstance(n, ast.ImportFrom):
                    record['imports'].append('.'*n.level+(n.module or ''))
                if isinstance(n, ast.Call):
                    f = ast.unparse(n.func)
                    record['calls'].append({'line':n.lineno,'target':f})
                    if f.endswith(('import_module','spec_from_file_location','__import__')):
                        record['dynamic_imports'].append({'line':n.lineno,'expr':ast.unparse(n)})
            mod = name.removeprefix('src/').removesuffix('.py').replace('/','.').replace('autonomy-fabric','autonomy_fabric').replace('design-intelligence','design_intelligence').replace('model-fabric','model_fabric').removesuffix('.__init__')
            record['module'] = mod
            modules[mod] = record
        except SyntaxError as e:
            record['ast'] = f'SYNTAX_ERROR:{e.lineno}'
    records.append(record)
edges = []
for mod, rec in modules.items():
    for imp in rec['imports']:
        resolved = imp
        if imp.startswith('.'):
            level = len(imp)-len(imp.lstrip('.'))
            resolved = '.'.join(mod.split('.')[:-level])+'.'+imp.lstrip('.')
        if resolved in modules:
            edges.append(dict(caller=rec['path'],callee=modules[resolved]['path'],kind='STATIC_IMPORT'))
    rec['consumers'] = [e['caller'] for e in edges if e['callee']==rec['path']]
for rec in records:
    rec['consumers'] = sorted(set(e['caller'] for e in edges if e['callee']==rec['path']))
prod = [r for r in records if r['path'].endswith('.py') and '/tests/' not in r['path'] and not r['path'].startswith('tests/')]
stats = dict(base_sha=ref,tracked_files=len(records),tracked_bytes=sum(r['bytes'] for r in records),python_files=len(modules),production_python_files=len(prod),production_python_lines=sum(r['lines'] for r in prod),ast_parsed=sum(r.get('ast')=='PARSED' for r in records),categories=dict(collections.Counter(r['category'] for r in records)),static_import_edges=len(edges),dynamic_import_sites=sum(len(r.get('dynamic_imports',[])) for r in records),limitations=['Static imports are not runtime call proof.','No static consumer does not establish dead code. CLI, dynamic imports, external scripts are separate roots.','Full byte/AST coverage is not full semantic review.','Windows linked worktree graph is unavailable.'])
for name,value in [('baseline_inventory.json',records),('import_edges.json',edges),('coverage.json',stats)]:
    (OUT/name).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
with (OUT/'baseline_inventory.csv').open('w',newline='') as f:
    cols=['path','category','bytes','lines','sha256','ast','purpose','definitions','consumers','dynamic_imports']
    w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(records)
print(json.dumps(stats,indent=2))
