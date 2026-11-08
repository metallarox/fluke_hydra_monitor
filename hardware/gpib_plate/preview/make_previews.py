#!/usr/bin/env python3
"""Light STL models for GitHub's interactive 3D viewer (not for printing).

    python3 hardware/gpib_plate/preview/make_previews.py      (needs: pip install cadquery)

Writes, next to this file:
  assembly.stl           plate + cover mounted, with Wemos and MAX3232 placeholders
  exploded.stl           the same, exploded along Z

and ../3D.md, a page that embeds the models (```stl blocks render in 3D on GitHub).
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
m = {}
exec(compile(open(os.path.join(HERE, '..', 'hydra_wemos_plate.FCMacro')).read(), 'plate', 'exec'), m)
P = dict(m['P'], text='')                 # no engraving: far fewer triangles
d = m['derived'](P)
B, R, U = m['box'], m['crect'], m['union']
import cadquery as cq

plate, cover = m['make_plate'](P), m['make_cover'](P)
zb = d['zb']
wemos = U([R(0, 0, P['wemos_l'], P['wemos_w'], zb, zb + 1.0, 0.5),
           B(P['wemos_l'] / 2 - 24, -8, zb + 1, P['wemos_l'] / 2, 8, zb + 4.2)])
z0 = -P['tongue_depth'] + P['tongue_end'] + 0.2
module = U([B(-P['module_l'] / 2, 0.2, z0, P['module_l'] / 2, 1.2, z0 + P['module_w']),
            B(-5, -1.55, z0 + 3.5, 5, 0.2, z0 + 9.5)])
T = lambda s, dz: s.translate(cq.Vector(0, 0, dz))


def save(shape, name, tol, ascii_=False):
    path = os.path.join(HERE, name)
    shape.exportStl(path, tolerance=tol, angularTolerance=0.5, ascii=ascii_)
    return path


save(cq.Compound.makeCompound([plate, cover, wemos, module]), 'assembly.stl', 0.08)
save(cq.Compound.makeCompound([plate, T(module, 34), T(wemos, 40), T(cover, 58)]), 'exploded.stl', 0.08)
def compact_ascii(shape, tol):
    """ASCII STL with 2-decimal coordinates: small enough to embed in a Markdown page."""
    verts, tris = shape.tessellate(tol, 1.0)
    out = ['solid m']
    for a, b, c in tris:
        p0, p1, p2 = verts[a], verts[b], verts[c]
        n = (p1 - p0).cross(p2 - p0)
        ln = n.Length or 1.0
        out.append('facet normal %.2f %.2f %.2f\nouter loop' % (n.x / ln, n.y / ln, n.z / ln))
        for v in (p0, p1, p2):
            out.append('vertex %.2f %.2f %.2f' % (v.x, v.y, v.z))
        out.append('endloop\nendfacet')
    out.append('endsolid m')
    return '\n'.join(out), len(tris)


blocks = []
for name, shape, tol in [('exploded', cq.Compound.makeCompound([plate, T(module, 34), T(wemos, 40), T(cover, 58)]), 0.4),
                         ('plate', plate, 0.7), ('cover', cover, 0.7)]:
    txt, n = compact_ascii(shape, tol)
    blocks.append((name, txt))
    print(name, n, 'triangles')

titles = {'exploded': 'Exploded: plate, MAX3232 (in the tongue), Wemos, cover',
          'plate': 'Plate: the tongue enters the GPIB slot, the MAX3232 stands in its pocket',
          'cover': 'Cover: clamped by the two GPIB screws, presses the Wemos corners'}
with open(os.path.join(HERE, '..', '3D.md'), 'w') as f:
    f.write('# GPIB plate — 3D views\n\nDrag to rotate, scroll to zoom. Simplified meshes for viewing: '
            'print the STL files in the [parent folder](README.md).\n\n')
    for name, txt in blocks:
        f.write(f'## {titles[name]}\n\n```stl\n{txt.strip()}\n```\n\n')
for name, txt in blocks:
    print(name, len(txt) // 1024, 'KB ascii')
