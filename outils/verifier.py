#!/usr/bin/env python3
"""Vérifications de l'app Mon Patrimoine : extrait le script principal de index.html
pour `node --check`, compile les scripts Python et contrôle quelques règles de la page."""
import pathlib, py_compile, re, subprocess, sys

racine = pathlib.Path(__file__).resolve().parent.parent
page = (racine / 'index.html').read_text(encoding='utf-8')
erreurs = []

scripts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', page, re.S)
if not scripts:
    erreurs.append('aucun script inline trouvé dans index.html')
else:
    tmp = racine / '.verif-main.js'
    tmp.write_text(max(scripts, key=len), encoding='utf-8')
    r = subprocess.run(['node', '--check', str(tmp)], capture_output=True, text=True)
    tmp.unlink()
    if r.returncode != 0:
        erreurs.append('syntaxe JavaScript :\n' + r.stderr)

if '<title>' not in page[:8192]:
    erreurs.append('<title> absent des 8 premiers Ko')
taille = len(page.encode('utf-8'))
if taille > 16 * 1024 * 1024:
    erreurs.append(f'page trop lourde : {taille} octets')
hotes = set(re.findall(r'<script[^>]+src="https://([^/"]+)', page))
autorises = {'cdnjs.cloudflare.com', 'cdn.jsdelivr.net', 'unpkg.com', 'cdn.tailwindcss.com', 'code.jquery.com'}
if hotes - autorises:
    erreurs.append('scripts chargés depuis des hôtes non autorisés : ' + ', '.join(sorted(hotes - autorises)))

for py in sorted(racine.rglob('*.py')):
    try:
        py_compile.compile(str(py), doraise=True)
    except py_compile.PyCompileError as e:
        erreurs.append(f'{py.relative_to(racine)} : {e.msg}')

if erreurs:
    print('ÉCHEC\n- ' + '\n- '.join(erreurs))
    sys.exit(1)
print(f'OK : index.html ({taille // 1024} Ko), script principal valide, {len(list(racine.rglob("*.py")))} scripts Python compilés')
