"""Isolated real AppKit dispatch tests. Caller guards Quartz and config."""
from unittest.mock import patch
from AppKit import NSApplication, NSSegmentedControl, NSSlider, NSMakePoint
import hid_bridge as HB
import dptouch_display as DP
import dptouch_engine as E
import dptouch_window as UI


def verify_native_controls(app, report):
    win = app.win
    saved_cfg = dict(app.cfg)
    saved_page = win.page
    saved_tgt = getattr(app, '_disp_tgt', None)
    saved_opts = getattr(app, '_disp_opts', [])
    dispatch = NSApplication.sharedApplication().sendAction_to_from_
    def fire(control):
        assert dispatch(control.action(), control.target(), control)
    try:
        import sys
        module = sys.modules[app.__class__.__module__]
        defaults = module.DEFAULTS
        assert defaults['edid_correct'] is True, 'verified geometry must default to automatic'
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / 'config.json'
            for raw, expected in (({}, True), ({'edid_correct': False}, False), ({'edid_correct': True}, True)):
                config.write_text(json.dumps(raw))
                with patch.object(module, 'CFG_PATH', str(config)), patch.object(module, '_migrate_legacy_paths'):
                    assert module.load_cfg()['edid_correct'] is expected
                assert json.loads(config.read_text()) == raw
        win.page = 'touch'
        win._relayout(animate=False)
        assert '设备位置修正' in win.c['edid_status'].stringValue()
        forbidden = ('测量', '标定', '校准', '无需逐人')
        def audit_view(view):
            for method in ('title', 'stringValue', 'toolTip'):
                try:
                    text = str(getattr(view, method)() or '')
                except (AttributeError, TypeError):
                    continue
                assert not any(word in text for word in forbidden), (method, text)
            if hasattr(view, 'action'):
                assert 'calibrat' not in str(view.action()).lower()
            for child in view.subviews():
                audit_view(child)
        for page in UI.PAGES:
            win.page = page[0]
            win._relayout(animate=False)
            assert not {'calibrate', 'calibration_hint'} & win.c.keys()
            audit_view(win.win.contentView())
        audit_view(win.adv.win.contentView())
        def audit_menu(menu):
            for item in menu.itemArray():
                assert not any(word in str(item.title()) for word in forbidden), item.title()
                assert 'calibrat' not in str(item.action()).lower()
                if item.submenu():
                    audit_menu(item.submenu())
        audit_menu(app.item.menu())
        with patch.object(app, 'start_calibration') as retired:
            assert dispatch('onCalibrate:', win, None)
            retired.assert_not_called()
        # Retired research module is not shipped; stale selectors remain no-ops.
        assert app.start_calibration() is False
        app._calibration_path = 'must-not-analyze.json'
        assert app._poll_calibration() is None
        app._calibration_path = None
        win.page = 'touch'
        win._relayout(animate=False)
        for state in ('disabled', 'unsupported', 'ambiguous', 'checking', 'failed'):
            text = win._edid_status_text(app, dict(state=state, detail='fixture'))[0]
            assert '已生效' not in text, (state, text)
        text = win._edid_status_text(app, dict(state='applied', readback_after=(282,188)))[0]
        assert '已生效' in text and '282.0' in text
        report('位置修正 AppKit: 默认自动/真实状态/测量入口全部撤除且启动/分析 fail-closed 通过')
        win.page = 'gestures'
        win._relayout(animate=False)
        assert isinstance(win.c['nat'], NSSegmentedControl)
        assert isinstance(win.c['gain'], NSSlider)
        for index, gain in enumerate(E.GAINS):
            win.c['gain'].selectItemAtIndex_(index)
            fire(win.c['gain'])
            assert app.cfg['gain'] == gain[0]
            assert win.c['gain_value'].stringValue().startswith('%g×' % gain[0])
        fire(win.c['gain_default'])
        assert app.cfg['gain'] == 1.0
        assert not win.c['gain_default'].isEnabled()
        assert '默认' in win.c['gain_value'].stringValue()
        for row, (gesture, _, options, _) in enumerate(HB.BIND_GESTURES):
            for index, action in enumerate(options):
                ctrl = win.c['bind_' + gesture]
                assert not ctrl.isBordered()
                ctrl.selectItemAtIndex_(index)
                fire(ctrl)
                assert app.cfg[HB.bind_key(gesture)] == action
                assert ctrl.indexOfSelectedItem() == index
        assert not UI.check_layout(win.win.contentView(), UI.W, UI.H)
        report('原生控件 AppKit: 方向分段/全部增益/精确默认/全部分组动作保存 通过')
        win.page = 'display'
        win._relayout(animate=False)
        screens = [dict(id=701,name='离线 fixture A',bounds=(-1920,0,1920,1080),main=True,builtin=True,logical=(1920,1080)),
                   dict(id=702,name='离线 fixture B',bounds=(0,-200,2560,1440),main=False,builtin=False,logical=(2560,1440))]
        with patch.object(DP, 'displays', return_value=screens), patch.object(app.edid, 'tick'), patch.object(app.edid, 'reconfigure'), patch.object(app.edid, 'set_desired'):
            win.refresh(force=True)
            preview = win.c['monitor_preview']
            assert len(preview.subviews()) == 2
            for ctrl in list(preview.subviews()):
                point = ctrl.convertPoint_toView_(NSMakePoint(ctrl.bounds().size.width/2, ctrl.bounds().size.height/2), win.win.contentView())
                hit = win.win.contentView().hitTest_(point)
                assert hit is ctrl, (hit, ctrl)
                fire(ctrl)
                assert app.cfg['target_display'] == str(ctrl.tag())
                assert app.engine.target['id'] == ctrl.tag()
            fire(win.c['target_auto'])
            assert app.cfg['target_display'] == 'auto'
            screens.clear()
            app.engine._sync_target()
            win.refresh(force=True)
            assert not preview.subviews()
            assert preview.data == ([],None)
        report('屏幕排列 AppKit: 原生按钮 hitTest/目标 A B/自动/离线删除/菜单键盘等价入口 通过')
        # All display changes stay behind the existing app method and a fixture.
        current = dict(w=1704,h=1136,pw=3408,ph=2272,hz=60,hidpi=True)
        app._disp_tgt = dict(id=999,name='离线 fixture 平板屏')
        app._disp_opts = [dict(current),dict(w=1280,h=800,pw=2560,ph=1600,hz=60,hidpi=True)]
        with patch.object(app, '_sync_display'), patch.object(DP,'current',side_effect=lambda _:dict(current)), patch.object(DP,'panel_native',return_value=(3408,2272)), patch.object(app,'apply_display_index') as apply:
            fire(win.c['mode_change'])
            assert win.mode_panel.isVisible()
            assert '1704 × 1136' in win.c['disp_summary'].stringValue()
            assert all(title.count('Hz') == 1 for title in win.c['disp'].titles), win.c['disp'].titles
            assert not win.c['mode_apply'].isEnabled()
            win.c['disp'].selectItemAtIndex_(1)
            fire(win.c['disp'])
            assert apply.call_count == 0
            assert win.c['mode_apply'].isEnabled()
            assert '待应用' in win.c['mode_status'].stringValue()
            win._sync_display()
            assert win.c['disp'].indexOfSelectedItem() == 1
            fire(win.c['mode_apply'])
            apply.assert_called_once_with(1)
            assert '未应用' in win.c['mode_status'].stringValue()
            apply.side_effect = lambda index: current.update(app._disp_opts[index])
            fire(win.c['mode_apply'])
            assert not win.c['mode_apply'].isEnabled()
            assert '当前已应用' in win.c['mode_status'].stringValue()
            assert win.c['mode_apply'].keyEquivalent() == '\r'
            assert win.c['mode_cancel'].keyEquivalent() == '\x1b'
            assert not UI.check_layout(win.mode_panel.contentView(),560,410)
            fire(win.c['mode_cancel'])
            assert not win.mode_panel.isVisible()
            app._disp_tgt = None
            win._sync_display()
            assert not win.c['mode_change'].isEnabled()
            assert not win.c['mode_apply'].isEnabled()
        report('分辨率面板 AppKit: 实际原生列表/选择零切档/待应用保持/失败/成功回读/禁用/Return Escape/布局 通过 · 切档边界 fixture 拦截')
    finally:
        app._disp_tgt, app._disp_opts = saved_tgt, saved_opts
        app.cfg = saved_cfg
        app.engine.update(**saved_cfg)
        win.page = saved_page
        if getattr(win,'mode_panel',None): win.mode_panel.orderOut_(None)
        win._relayout(animate=False)
