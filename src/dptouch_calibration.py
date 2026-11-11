"""Stdlib-only offline analysis. Never applies a correction to macOS."""
import argparse
import json
import math
import random
import statistics as st


def forward(model, p):
    return [sum(a*x for a,x in zip(row,p))+b for row,b in zip(model['A'],model['b'])]


def inverse(model, p):
    (a,b),(c,d)=model['A']; det=a*d-b*c
    if not math.isfinite(det) or abs(det)<1e-8: raise ValueError('singular affine')
    x,y=[v-t for v,t in zip(p,model['b'])]
    return [(d*x-b*y)/det,(-c*x+a*y)/det]


def _solve(matrix, vector):
    a=[list(row)+[v] for row,v in zip(matrix,vector)]
    for i in range(3):
        k=max(range(i,3),key=lambda k:abs(a[k][i])); a[i],a[k]=a[k],a[i]
        if abs(a[i][i])<1e-9: raise ValueError('degenerate target geometry')
        z=a[i][i]; a[i]=[v/z for v in a[i]]
        for k in range(3):
            if k!=i:
                z=a[k][i]; a[k]=[v-z*w for v,w in zip(a[k],a[i])]
    return [row[3] for row in a]


def _fit(groups):
    # Equal target weight: repeats do not change spatial leverage.
    x=[list(g[0]['target'])+[1.] for g in groups]
    y=[[st.median(s['touch'][j] for s in g) for j in range(2)] for g in groups]
    w=[1.]*len(x)
    for _ in range(12):
        normal=[[sum(wi*xi[a]*xi[b] for wi,xi in zip(w,x)) for b in range(3)] for a in range(3)]
        coef=[_solve(normal,[sum(wi*xi[a]*yi[j] for wi,xi,yi in zip(w,x,y)) for a in range(3)]) for j in range(2)]
        residual=[math.dist([sum(c*v for c,v in zip(row,xi)) for row in coef],yi) for xi,yi in zip(x,y)]
        scale=max(.25,1.4826*st.median(residual)); w=[min(1.,1.5*scale/max(e,1e-12)) for e in residual]
    model={'A':[c[:2] for c in coef],'b':[c[2] for c in coef]}
    inverse(model,[0,0])
    return model


def _metrics(model,offset,rows):
    return {'identity_mean':st.mean(math.dist(s['touch'],s['target']) for s in rows),
            'constant_offset_mean':st.mean(math.dist([v-b for v,b in zip(s['touch'],offset)],s['target']) for s in rows),
            'candidate_mean':st.mean(math.dist(inverse(model,s['touch']),s['target']) for s in rows)}


def analyze(data,bootstrap=400,seed=1729):
    if data.get('status','completed_pending_analysis') not in ('completed_pending_analysis','closed'): raise ValueError('incomplete or cancelled session')
    if data.get('synthetic') is True: raise ValueError('synthetic UI audit is not human measurement')
    if bootstrap<1: raise ValueError('bootstrap must be positive')
    if data.get('schema_version')!=1 or data.get('state')!='B_current' or data.get('coordinate_space')!='view_points_bottom_left': raise ValueError('unsupported schema/state/coordinates')
    d=data.get('display',{})
    if d.get('vendor')!=25001 or d.get('model')!=4 or d.get('name')!='MI DISPLAY' or d.get('builtin') is not False or d.get('panel_pixels')!=[3408,2272]: raise ValueError('wrong display')
    if any(e.get('kind') == 'cancelled' for e in data.get('contact_events', [])):
        raise ValueError('cancelled contact invalidates session even after window close')
    if data.get('invalidated'):
        raise ValueError('mapping or geometry changed during collection')
    if data.get('pending_contact'):
        raise ValueError('unreleased pending contact')
    schedule=data.get('order'); declared=data.get('targets')
    if schedule is not None and len(schedule)!=len(data['samples']):
        raise ValueError('incomplete declared schedule')
    groups={}; contacts=set(); last_end=-math.inf
    for sample_index,s in enumerate(data['samples']):
        if declared is not None:
            try:
                target_index=int(s['target_id'])
                if target_index<0 or target_index>=len(declared) or s['target']!=declared[target_index]:
                    raise ValueError('sample does not match declared target')
                if s['split']!=('train' if target_index<5 else 'validation'):
                    raise ValueError('sample does not match declared split')
                if schedule is not None and schedule[sample_index]!=target_index:
                    raise ValueError('sample does not match intended target schedule')
            except (TypeError,IndexError):
                raise ValueError('invalid declared target') from None
        if s['geometry_id']!=data['geometry_id']: raise ValueError('window geometry changed')
        if 'geometry' in data and s.get('geometry')!=data['geometry']: raise ValueError('inconsistent view/window/screen coordinates')
        if s.get('touch_type',0)!=0: raise ValueError('non-direct sample')
        if s['split'] not in ('train','validation'): raise ValueError('invalid split')
        if any(len(s[k])!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in s[k]) for k in ('target','touch')): raise ValueError('non-finite point')
        if s['contact_id'] in contacts or not math.isfinite(s['began']) or not math.isfinite(s['ended']) or s['began']<last_end or s['ended']<=s['began']: raise ValueError('not distinct released contacts')
        gate_end=s.get('release_gate_ended',s['ended'])
        if not math.isfinite(gate_end) or gate_end<s['ended']:
            raise ValueError('invalid contact release gate')
        contacts.add(s['contact_id']); last_end=gate_end
        key=s['target_id']; g=groups.setdefault(key,[])
        if g and (s['target']!=g[0]['target'] or s['split']!=g[0]['split']): raise ValueError('inconsistent target label')
        g.append(s)
    train=[g for g in groups.values() if g[0]['split']=='train']; val=[g for g in groups.values() if g[0]['split']=='validation']
    if len(train)<5 or len(val)<4 or any(len(g)<(5 if g[0]['split']=='train' else 3) for g in groups.values()): raise ValueError('insufficient repeats: collect_more')
    if set(tuple(g[0]['target']) for g in train)&set(tuple(g[0]['target']) for g in val): raise ValueError('holdout overlaps training')
    model=_fit(train)
    offset=[st.median(st.median(s['touch'][j]-s['target'][j] for s in g) for g in train) for j in range(2)]
    rows=[s for g in val for s in g]; metrics=_metrics(model,offset,rows)
    spreads={g[0]['target_id']:st.median(math.dist(s['touch'],[st.median(t['touch'][j] for t in g) for j in range(2)]) for s in g) for g in groups.values()}
    rng=random.Random(seed); gains=[]
    for _ in range(bootstrap):
        # Resample contacts WITHIN fixed targets; never use holdout for fitting.
        bt=[[rng.choice(g) for _ in g] for g in train]; bv=[rng.choice(g) for g in val for _ in g]
        bm=_fit(bt); bo=[st.median(st.median(s['touch'][j]-s['target'][j] for s in g) for g in bt) for j in range(2)]
        m=_metrics(bm,bo,bv); gains.append(min(m['identity_mean'],m['constant_offset_mean'])-m['candidate_mean'])
    gains.sort(); ci=[gains[int((len(gains)-1)*q)] for q in (.025,.975)] if gains else [None,None]
    improvement=min(metrics['identity_mean'],metrics['constant_offset_mean'])-metrics['candidate_mean']
    rec='candidate_needs_independent_verification' if improvement>1 and ci[0] is not None and ci[0]>0 else 'no_extra_correction'
    return {'recommended':rec,'model':model,'constant_offset':offset,'validation':metrics,'bootstrap_improvement_95pct':ci,'bootstrap_replicates':bootstrap,'raw_sample_count':len(data['samples']),
            'downweighted_train_targets':sum(any(math.dist(s['touch'],[st.median(t['touch'][j] for t in g) for j in range(2)])>max(2,3*spreads[g[0]['target_id']]) for s in g) for g in train),
            'random_aiming_plus_sensor_spread_median_pt':spreads,'identifiability':'Fixed finger aiming bias and constant system translation are not identifiable. Intercept is NOT hardware bias. Interpretation assumes zero-mean targeting error; repeats cannot verify that assumption.',
            'application':'Offline candidate only. Native OS touch does not consume this Python affine map; EDID cannot implement arbitrary affine translation. No automatic application.',
            'uncertainty_scope':'Conditional within-target repeat uncertainty, not uncertainty in fixed aiming bias, spatial generalization, or future sessions. Validation used only for final evaluation, never fitting or model selection.'}


def find_calibration_app():
    """Find the bundled native sampler without depending on a checkout path."""
    import os, sys
    from pathlib import Path
    candidates=[]
    if getattr(sys,'_MEIPASS',None):
        candidates.append(Path(sys._MEIPASS)/'TouchCalibration.app')
    executable=Path(sys.executable)
    if executable.parent.name=='MacOS':
        candidates.append(executable.parent.parent/'Resources'/'TouchCalibration.app')
    candidates.append(Path(__file__).resolve().parent.parent/'tools/touch_calibration/build/TouchCalibration.app')
    for app in candidates:
        binary=app/'Contents/MacOS/TouchCalibration'
        if binary.is_file() and os.access(binary,os.X_OK):
            return str(app)
    return None


def launch_calibration(state_dir):
    """Launch through LaunchServices. This function never applies a mapping."""
    import os, subprocess, uuid
    from pathlib import Path
    app=find_calibration_app()
    if not app:
        raise RuntimeError('重复测量工具未打包')
    directory=Path(state_dir)/'Calibration'
    directory.mkdir(parents=True,exist_ok=True)
    output=directory/(str(uuid.uuid4())+'.json')
    result=subprocess.run(['/usr/bin/open','-n',app,'--args','--output',str(output)],
                          capture_output=True,text=True,timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or '无法启动重复测量工具')
    return str(output)


def analyze_completed(path, bootstrap=400):
    """Return None while collecting; validate and atomically save a final report."""
    import os, tempfile
    from pathlib import Path
    source=Path(path)
    if not source.exists():
        return None
    with source.open() as f:
        data=json.load(f)
    if data.get('status')=='collecting':
        return None
    try:
        result=analyze(data,bootstrap=bootstrap)
    except (ValueError,KeyError,TypeError) as exc:
        result={'recommended':'collect_more','error':str(exc)}
    output=source.with_name(source.stem+'-analysis.json')
    fd,tmp=tempfile.mkstemp(dir=str(output.parent),prefix='.analysis-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,output)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    result['report_path']=str(output)
    return result


def main():
    p=argparse.ArgumentParser(); p.add_argument('input'); p.add_argument('--output'); p.add_argument('--bootstrap',type=int,default=400); a=p.parse_args()
    try:
        with open(a.input) as f: result=analyze(json.load(f),bootstrap=a.bootstrap)
    except (ValueError,KeyError,TypeError) as e: result={'recommended':'collect_more','error':str(e)}
    text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if a.output:
        import os, tempfile
        directory=os.path.dirname(os.path.abspath(a.output)); fd,tmp=tempfile.mkstemp(dir=directory,prefix='.analysis-')
        with os.fdopen(fd,'w') as f: f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,a.output)
    print(text)

if __name__=='__main__': main()
