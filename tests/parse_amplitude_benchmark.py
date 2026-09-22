"""Parse a completed firmware experiment UART transcript into a reviewable JSON."""
import json
from pathlib import Path
import re
import sys


def parse(path):
    rows=[]
    current=None
    complete=False
    for line in Path(path).read_text(errors='replace').splitlines():
        if 'BENCH begin ' in line:
            current=dict(re.findall(r'(\w+)=([^\s]+)',line))
            current['stage_us']={}
        elif 'amp result=' in line and current is not None:
            current['amplitude']=dict(re.findall(r'(\w+)=([^\s]+)',line))
        elif 'amp timing stage=' in line and current is not None:
            m=re.search(r'stage=(\w+) total=(\d+)us',line)
            if m: current['stage_us'][m[1]]=int(m[2])
        elif 'BENCH end ' in line:
            assert current is not None
            fields=dict(re.findall(r'(\w+)=([^\s]+)',line))
            assert fields['name']==current['name']
            for k,v in fields.items(): current[k]=v if k=='name' else int(v)
            rows.append(current); current=None
        elif 'BENCH COMPLETE' in line:
            complete=True
    assert complete and len(rows)==11, 'incomplete benchmark'
    assert all(row['status']==0 for row in rows), 'variant failed'
    assert len({row['fingerprint'] for row in rows})==1, 'input changed'
    assert rows[-1]['name']=='baseline_repeat' and rows[-1]['equal_baseline']==1
    assert next(row for row in rows if row['name']=='stream')['equal_baseline']==1
    return rows

if __name__=='__main__':
    Path(sys.argv[2]).write_text(json.dumps(parse(sys.argv[1]),indent=2)+'\n')
