"""Disposable source-wheel or exact published-package native qualification.

The test owns its random passwords and native accounts, and never retains them.
Operator crypto stays in canonical Syn; Potbot supplies the sandbox primitive.
"""
from __future__ import annotations
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
from uuid import uuid4
from contextlib import ExitStack
from syn_client.operator_protocol import canonical

import argparse
import importlib
import re

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--consumer-dir',type=Path,required=True)
parser.add_argument('--consumer-sha',required=True)
parser.add_argument('--runtime-root',type=Path,required=True)
parser.add_argument('--source-wheels',type=Path)
parser.add_argument('--official-store',type=Path)
parser.add_argument('--official-policy',type=Path)
parser.add_argument('--consumer-python',type=Path)
parser.add_argument('--local-disposable-fixture',action='store_true')
parser.add_argument('--lan-enrollment',action='store_true')
parser.add_argument('--group-rollover',action='store_true')
ARGS=parser.parse_args()
if os.getuid()!=0 or sys.platform!='linux':
    raise SystemExit('Actual restricted native Agent qualification requires disposable Linux root')
if os.environ.get('GITHUB_ACTIONS')!='true' and not ARGS.local_disposable_fixture:
    raise SystemExit('Explicit disposable-fixture scope is required outside CI')
if not re.fullmatch('[0-9a-f]{40}',ARGS.consumer_sha):
    raise SystemExit('An exact reviewed Potbot consumer SHA is required')
SYN=Path(__file__).resolve().parents[1]
POTBOT=ARGS.consumer_dir.resolve(strict=True)
actual=subprocess.check_output(['git','-c','safe.directory='+str(POTBOT),'-C',str(POTBOT),'rev-parse','HEAD'],text=True).strip()
if actual!=ARGS.consumer_sha or subprocess.check_output(['git','-c','safe.directory='+str(POTBOT),'-C',str(POTBOT),'status','--porcelain']):
    raise SystemExit('Consumer differs from its exact clean reviewed source')
sys.path.insert(0, str(POTBOT/'src'))
sys.path.insert(0, str(SYN/'scripts'))
provision=importlib.import_module('potbot.syn_release.agent_host').provision
prepared_launch=importlib.import_module('potbot.syn_release.agent_sandbox').prepared_launch
AgentSession=importlib.import_module('potbot.syn_release.agent').AgentSession
native_fixture=importlib.import_module('ci_operator_native').native_fixture
runtimeContract=importlib.import_module('syn.release').runtimeContract
initialize=importlib.import_module('syn.operator.service').initialize
operate=importlib.import_module('syn.operator.adoption_cli').operate
reserve_offer=importlib.import_module('syn.agent.enrollment').reserve_offer
seal_group_key=importlib.import_module('syn.agent.group').seal_group_key
OperatorServer=importlib.import_module('syn.operator.service').OperatorServer
EnrollmentJournal=importlib.import_module('syn.operator.adoption_state').EnrollmentJournal
_agent_key_lock=importlib.import_module('syn.operator.custody')._agent_key_lock
agent_status=importlib.import_module('syn_client.agent').agent_status

ROOT = Path('/var/lib/potbot/syn-agent-qualification')
RUNTIME = ARGS.runtime_root.resolve(strict=True)
PYTHON_PREFIX=Path(subprocess.check_output([str(RUNTIME/'bin/python'),'-I','-B','-c','import sys;print(sys.base_prefix)'],text=True,timeout=10).strip()).resolve(strict=True)
GROUP_ID = 'be31002b-b7a6-4e46-bd29-4e39fb04e31e'


def cleanup(host):
    """Only this process's proven, disposable fixture account and exact tree."""
    import pwd
    path = Path(host['home']).parent
    assert path.parent == ROOT and path.name == host['agent_id'].replace('-', '')
    assert json.loads((path/'host.json').read_bytes()) == host
    record = pwd.getpwnam(host['account'])
    assert record.pw_uid == host['uid'] and record.pw_dir == host['home']
    running = subprocess.run(['/usr/bin/pgrep','-u',str(host['uid'])], capture_output=True)
    assert running.returncode == 1, 'Owned fixture processes still exist; cleanup refused'
    result = subprocess.run(['/usr/sbin/userdel',host['account']], capture_output=True, timeout=10)
    assert result.returncode == 0, 'Owned disposable account removal refused'
    for policy in (Path('/etc/subuid'),Path('/etc/subgid')):
        original = policy.read_bytes()
        line = f"root:{host['uid']}:1".encode()
        assert original.splitlines().count(line) == 1
        policy.write_bytes(b'\n'.join(row for row in original.splitlines() if row != line)+b'\n')
    assert path.resolve().is_relative_to(ROOT.resolve())
    shutil.rmtree(path)


def independent_process(node, proof):
    pending=[node.process.pid]
    visited=set()
    matched=[]
    while pending:
        pid=pending.pop()
        if pid in visited:
            continue
        visited.add(pid)
        assert len(visited)<=64
        try:
            path=Path(f'/proc/{pid}')
            cmd=(path/'cmdline').read_bytes()
            if b'-m\x00syn.agent\x00serve\x00' in cmd:
                matched.append(path)
            pending.extend(map(int,(path/f'task/{pid}/children').read_text().split()))
        except FileNotFoundError:
            continue
    assert len(matched)==1
    path=matched[0]
    status=dict(row.split(':',1) for row in (path/'status').read_text().splitlines())
    uid=node.host['uid']
    assert list(map(int,status['Uid'].split()))==[uid]*4
    assert list(map(int,status['Gid'].split()))==[uid]*4
    assert int(status['NSpid'].split()[-1])==proof['status']['pid']
    assert all(int(status[name].strip(),16)==0 for name in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))
    assert status['NoNewPrivs'].strip()=='1' and status['Seccomp'].strip()=='2'
    namespaces={name:os.readlink(path/f'ns/{name}') for name in ('pid','mnt','net','user','ipc','uts','cgroup')}
    assert namespaces==node.initial['scope']['namespaces']
    assert all(value!=os.readlink(f'/proc/self/ns/{name}') for name,value in namespaces.items())
    assert not (path/'root/run/user/1000/bus').exists()
    assert not (path/'root/home/synoperator').exists()
    assert (path/'root/tmp').stat().st_mode & 0o7777 == 0o1777
    limits=(path/'limits').read_text()
    assert '1073741824' in limits and '16777216' in limits
    return {'native_uid_verified':uid,'native_pid':int(path.name),'namespaces':namespaces,
        'no_capabilities':True,'no_new_privileges':True,'seccomp_filter_active':True,
        'operator_home_and_bus_hidden':True,'memory_and_file_limits_measured':True,
        'private_temporary_directory_sticky_writable':True}


class Session:
    def __init__(self, stack, host, expected_operator, *, password=None, existing=False, epoch=1):
        self.host = host
        self.master, slave = pty.openpty()
        self.password = password or secrets.token_urlsafe(32)
        self.closed = False
        self.terminal = bytearray()
        self.output = bytearray()
        self.answered = set()
        arguments = ('--state-dir',str(Path(host['home'])/'syn'), '--keyring-service','syn.agent.fixture',
            '--keyring-account','agent', '--expected-operator',expected_operator,'--network-id','r04-fixture',
            '--approved-device-profile','syn.host-bound-virtual-device/1','--group-id',GROUP_ID,'--group-epoch',str(epoch)) + (() if existing else ('--new-identity',))
        if ARGS.official_policy:
            if ARGS.consumer_python is None:
                raise AssertionError('Official acceptance must execute the full reviewed Potbot controller')
            argv=[str(ARGS.consumer_python),'-I','-B','-m','potbot','syn-agent','session',
                '--store',str(ARGS.official_store),'--policy',str(ARGS.official_policy),'--agent-root',str(ROOT),
                '--agent-id',host['agent_id'],'--uid',str(host['uid']),'--expected-operator',expected_operator,
                '--network-id','r04-fixture','--group-id',GROUP_ID,'--group-epoch',str(epoch),
                '--approved-device-profile','syn.host-bound-virtual-device/1']
            if not existing:
                argv.append('--new-identity')
            pass_fds=(slave,)
        else:
            self.launch = stack.enter_context(prepared_launch(host=host,python=RUNTIME/'bin/python',
                runtime_roots=tuple(dict.fromkeys((RUNTIME,PYTHON_PREFIX))),arguments=arguments))
            argv=self.launch.argv
            pass_fds=(*self.launch.pass_fds,slave)
        def terminal():
            os.setsid()
            fcntl.ioctl(slave,termios.TIOCSCTTY,0)
            os.tcsetpgrp(slave,os.getpgrp())
        self.process = subprocess.Popen(argv, pass_fds=pass_fds,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,preexec_fn=terminal)
        os.close(slave)
        stack.callback(self.close)
        self.initial = self.read()
        self.api = AgentSession(self.process)

    def read(self):
        deadline = time.monotonic()+25
        prompts = (b'Native keystore password (private, no echo): ',b'Confirm new native keystore password: ')
        while time.monotonic()<deadline:
            if b'\n' in self.output:
                raw, _, rest = self.output.partition(b'\n')
                self.output[:] = rest
                assert self.password.encode() not in raw
                return json.loads(raw)
            ready,_,_ = select.select([self.master,self.process.stdout],[],[],0.1)
            for fd in ready:
                try:
                    part = os.read(fd if isinstance(fd,int) else fd.fileno(),65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        part=b''
                    else:
                        raise
                if fd == self.master:
                    self.terminal.extend(part)
                    assert len(self.terminal)<16384
                    assert self.password.encode() not in self.terminal, 'Fixture password was echoed'
                    for index,prompt in enumerate(prompts):
                        if index not in self.answered and prompt in self.terminal:
                            os.write(self.master,self.password.encode()+b'\n')
                            self.answered.add(index)
                else:
                    self.output.extend(part)
                    assert len(self.output)<=65537
            if self.process.poll() is not None:
                error=self.process.stderr.read(4096).decode(errors='replace')
                assert self.password not in error
                raise AssertionError(f'Agent fixture exit={self.process.returncode}, prompts={sorted(self.answered)}, public_error={error}')
        raise AssertionError('Agent fixture response deadline exceeded')

    def command(self, operation, payload=None):
        return self.api.exchange(operation,payload)

    def close(self):
        if self.closed:
            return
        self.closed=True
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        os.close(self.master)


def main():
    from potbot.syn_release.store import _directory, SynReleaseStore
    from potbot.syn_release.contracts import Policy
    _directory(ROOT.parent,create=True)
    _directory(ROOT,create=True)
    if (ARGS.official_policy is None)!=(ARGS.official_store is None):
        raise AssertionError('Official qualification requires both independently pinned store and policy')
    if ARGS.official_policy:
        policy=Policy.parse(ARGS.official_policy.read_bytes())
        assert policy.value['mode']=='agent'
        store=SynReleaseStore(ARGS.official_store)
        verified=store.verify(policy)
        assert RUNTIME==store.root/'installs'/verified['directory']/'venv'
        assert Path(sys.prefix).resolve()==RUNTIME
        assert runtimeContract().to_dict()==verified['observed']['runtime']
    elif ARGS.source_wheels is None:
        raise AssertionError('A source-wheel qualification must retain exact candidate wheel digests')
    supports_rollover = 'group-member-revocation' in runtimeContract().capabilities
    if ARGS.group_rollover and (not ARGS.official_policy or not supports_rollover):
        raise AssertionError('Requested rollover requires pinned official packages with the rollover contract')
    if ARGS.lan_enrollment:
        from ci_lan_enrollment import enroll as enroll_lan, prepare_network
        if "authenticated-lan-enrollment" not in runtimeContract().capabilities:
            raise AssertionError("Selected installed runtime does not advertise LAN enrollment")
        prepare_network()
    hosts=[]
    lan_evidence=[]
    try:
        with tempfile.TemporaryDirectory(prefix='syn-r04-op-',dir='/tmp') as name, ExitStack() as stack:
            base=Path(name)
            base.chmod(0o700)
            stack.enter_context(native_fixture(base))
            operator=base/'operator'
            operator.mkdir(mode=0o700)
            created=initialize(operator,service='syn.fixture.'+secrets.token_hex(8),account='operator',network_id='r04-fixture')
            pin=created['identity']['base_public_key_hash']
            config=operator/'operator.json'
            operate(config,'adoption-init')
            if ARGS.lan_enrollment:
                from syn.operator import lan_inbox
                lan_inbox.initialize(config)
            nodes=[]
            for uid in (53102,53103):
                host=provision(ROOT,agent_id=uuid4(),uid=uid)
                hosts.append(host)
                node=Session(stack,host,pin)
                assert node.answered=={0,1}
                observed=node.initial['scope']
                assert observed['uid']==uid and observed['gid']==uid
                assert all(observed[key] for key in ('capabilities_zero','no_new_privileges','internet_socket_creation_denied','root_read_only'))
                if ARGS.lan_enrollment:
                    grant, network_receipt=enroll_lan(node,config,pin,uid)
                    lan_evidence.append(network_receipt)
                else:
                    offer=node.command('offer',operate(config,'agent-pulse'))
                    candidates=reserve_offer(config,offer,uid%256,approve=lambda _:True)
                    choice=node.command('choose',candidates)
                    grant=operate(config,'agent-grant',{'offer':offer,'candidates':candidates,'selection':choice},approve=lambda _:True)
                request=node.command('prepare',grant)
                sealed=operate(config,'agent-seal',{'grant':grant,'request':request})
                proof=node.command('adopt',{'grant':grant,'request':request,'envelope':sealed})
                receipt=operate(config,'agent-confirm',{'grant':grant,'key_proof':proof['key_proof']})
                server=OperatorServer(config)
                journal=EnrollmentJournal(server.directory,server.signer.service,server.signer.account,server.expected_hash)
                with _agent_key_lock(server.directory,server.signer.service,server.signer.account+'.operator'):
                    group=seal_group_key(server=server,journal=journal,grant_packet=grant,request_packet=request,
                        identifier=GROUP_ID,epoch=1,now=int(time.time()),approve=lambda _:True)
                node.command('adopt-group',{'grant':grant,'request':request,'group':group})
                node.command('activate',receipt)
                ready=node.command('serve')
                restarted=node.command('restart')
                outside=independent_process(node,restarted)
                observed=node.api.observe(restarted,owner_uid=uid)
                assert observed['native_uid']==uid and observed['native_pid']==outside['native_pid']
                assert ready['status']['selected_public_key']==restarted['status']['selected_public_key']
                assert ready['status']['boot_id']!=restarted['status']['boot_id']
                assert restarted['status']['boot']==ready['status']['boot']+1
                nodes.append((node,ready,restarted,outside))
                print('Real native encrypted adoption and node restart passed for disposable UID',uid,flush=True)
            assert nodes[0][1]['status']['selected_public_key']!=nodes[1][1]['status']['selected_public_key']
            assert nodes[0][0].initial['scope']['namespaces']['net']!=nodes[1][0].initial['scope']['namespaces']['net']
            assert nodes[0][1]['status']['group_certificate']['body']['group_key_commitment']==nodes[1][1]['status']['group_certificate']['body']['group_key_commitment']
            private_pubsub = None
            if 'authenticated-private-pubsub' in runtimeContract().capabilities:
                if ARGS.official_policy:
                    from ci_potbot_private_mesh import qualify
                    private_pubsub = qualify(nodes, base, root=ROOT, store=store, policy=policy)
                else:
                    from ci_potbot_private_mesh import qualify
                    private_pubsub = qualify(nodes, base, root=ROOT, source_runtime=RUNTIME,
                        source_contract=runtimeContract().to_dict())
            native=importlib.import_module('ci_operator_native')
            running_operator=native.start(operator)
            stack.callback(native.stop,running_operator)
            operator_proof=native.ready(running_operator,operator,created)
            assert running_operator.poll() is None
            assert operator_proof['status']['pid']==running_operator.pid
            assert all(os.readlink(f'/proc/{running_operator.pid}/ns/{name}')!=nodes[0][0].initial['scope']['namespaces'][name]
                for name in ('pid','mnt','net','user','ipc','uts','cgroup'))
            refused=False
            try:
                operate(config,'agent-pulse')
            except importlib.import_module('syn_client.operator_protocol').SynProtocolError:
                refused=True
            assert refused,'Serving and exclusive operator maintenance overlapped'
            complete=[]
            for node,first,second,outside in nodes:
                node.close()
                resumed=Session(stack,node.host,pin,password=node.password,existing=True)
                assert resumed.answered=={0}
                third=resumed.command('serve')
                assert third['status']['selected_public_key']==first['status']['selected_public_key']
                assert third['status']['boot']==second['status']['boot']+1
                assert resumed.initial['configuration_sha256']==node.initial['configuration_sha256']
                after=independent_process(resumed,third)
                resumed.api.observe(third,owner_uid=node.host['uid'])
                other=next(item for item in hosts if item!=node.host)
                assert not (Path(f"/proc/{after['native_pid']}/root")/other['home'].lstrip('/')).exists()
                checked=agent_status(socket_path=Path(node.host['home'])/'syn/agent.sock',owner_uid=node.host['uid'],
                    expected_hash=hashlib.sha256(bytes.fromhex(third['status']['selected_public_key'])).hexdigest(),
                    expected_operator=pin,expected_version=runtimeContract().runtime_version,configuration_sha256=third['status']['configuration_sha256'],
                    assignment_sha256=third['status']['assignment_sha256'],expected_group=GROUP_ID,expected_epoch=1)
                assert checked['status']['boot_id']==third['status']['boot_id']
                resumed.command('lock')
                resumed.process.wait(timeout=10)
                assert resumed.process.returncode==2
                assert not (Path(node.host['home'])/'syn/agent.sock').exists()
                complete.append({'uid':node.host['uid'],'scope':node.initial['scope'],
                    'initial':first,'node_restart':second,'full_native_session_restart':third,
                    'independent_host_observation':outside,'independent_restarted_host_observation':after,
                    'cross_agent_home_hidden':True,'locked_native_keystore_fenced_node':True})
            evidence=SYN/'artifacts/agent'
            evidence.mkdir(parents=True,exist_ok=True)
            if private_pubsub is not None:
                (evidence/'private-pubsub.json').write_bytes(canonical(private_pubsub))
            if ARGS.lan_enrollment:
                assert len({row["offer_sha256"] for row in lan_evidence}) == 2
                (evidence/'lan-enrollment.json').write_bytes(canonical({
                    "profile": "syn.lan-native-acceptance/1",
                    "official_release": ARGS.official_policy is not None,
                    "sandbox_internet_sockets_denied": True,
                    "two_distinct_agents_activated_and_restarted": True,
                    "agents": lan_evidence,
                }))
            if ARGS.group_rollover or (supports_rollover and not ARGS.official_policy):
                native.stop(running_operator)
                from ci_group_rollover import qualify_rollover
                rollover = qualify_rollover(
                    stack, Session, nodes, config,
                    official=(store, policy) if ARGS.official_policy else None,
                )
                (evidence/'group-rollover.json').write_bytes(canonical(rollover))
            wheelroot=ARGS.source_wheels
            receipt={'profile':('syn.agent-official-native-acceptance/1' if ARGS.official_policy else 'syn.agent-source-native-acceptance/1'),'result':'passed',
                'qualification':('official_released_native_software_custody_and_two_real_sandboxed_agents' if ARGS.official_policy else 'source_wheels_disposable_native_software_custody_and_two_real_sandboxed_agents'),
                'official_release_qualified':ARGS.official_policy is not None,'hardware_attested':False,'cog_execution_authorized':False,
                'operator':operator_proof,'exclusive_operator_maintenance_fenced':True,
                'consumer_sha':ARGS.consumer_sha,'runtime':runtimeContract().to_dict(), 'wheels':({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in wheelroot.glob('*.whl')} if wheelroot else {}),
                'official_release_pins':({'source_commit':policy.value['source_commit'],'manifest_sha256':policy.manifest_sha256} if ARGS.official_policy else None),
                'agents':complete}
            (evidence/'native.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print('PASS: two simultaneous canonical Agent processes; distinct native UID, selected key, private keyring and namespaces; stable identities across restart. See the retained receipt for the source or official release qualification tier.',flush=True)
    finally:
        for host in reversed(hosts):
            cleanup(host)


if __name__=='__main__':
    main()
