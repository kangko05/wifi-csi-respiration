"""Parse completed B1 firmware measurements (six fixed-input variants)."""
import json
from pathlib import Path
import re
import sys


def parse(path):
    rows=[]; kernels=[]; current=None; complete=False
    for line in Path(path).read_text(errors='replace').splitlines():
        fields=dict(re.findall(r'(\w+)=([^\s]+)',line))
        if 'FFT KERNEL ' in line:
            kernels.append({k:v if k=='mode' else int(v) for k,v in fields.items()})
        elif 'BENCH begin ' in line:
            current=fields; current['stage_us']={}
        elif 'amp result=' in line and current is not None:
            current['amplitude']=fields
        elif 'amp timing stage=' in line and current is not None:
            current['stage_us'][fields['stage']]=int(fields['total'].removesuffix('us'))
        elif 'BENCH end ' in line:
            assert current is not None and current['name']==fields['name']
            current.update({k:v if k=='name' else int(v) for k,v in fields.items()})
            rows.append(current); current=None
        elif 'BENCH COMPLETE' in line: complete=True
    assert complete and len(rows)==6 and len(kernels)==0
    assert all(r['status']==0 for r in rows+kernels)
    assert len({r['fingerprint'] for r in rows})==1
    assert rows[-1]['name']=='b1_float_repeat' and rows[-1]['equal_baseline']==1
    return dict(variants=rows,kernels=kernels)

if __name__=='__main__':
    Path(sys.argv[2]).write_text(json.dumps(parse(sys.argv[1]),indent=2)+'\n')
