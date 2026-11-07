"""Small source-owned AppKit diagrams. Read-only: never resolve or apply settings."""
import objc
from AppKit import (NSView, NSBezierPath, NSColor, NSFont, NSAttributedString,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSMakeRect,
                    NSImage, NSImageSymbolConfiguration)


def monitor_regions(infos, w, h):
    """Shared drawing and native button geometry in AppKit coordinates."""
    if not infos:
        return []
    bounds = [i.get('bounds', (0, 0, i.get('w', 1), i.get('h', 1))) for i in infos]
    minx = min(b[0] for b in bounds); miny = min(b[1] for b in bounds)
    maxx = max(b[0]+b[2] for b in bounds); maxy = max(b[1]+b[3] for b in bounds)
    scale = min((w-20)/max(1, maxx-minx), (h-30)/max(1, maxy-miny))
    return [(info['id'], (10+(b[0]-minx)*scale, 23+(maxy-b[1]-b[3])*scale,
                         max(3,b[2]*scale-3), max(3,b[3]*scale-3)))
            for info,b in zip(infos,bounds)]


class SettingPreview(NSView):
    """A bounded, accessible semantic diagram, not a second interactive control."""
    @objc.python_method
    def update(self, kind, data, description):
        self.kind, self.data, self.description = kind, data, description
        self.setAccessibilityElement_(True)
        self.setAccessibilityRole_("AXImage")
        self.setAccessibilityLabel_(description)
        self.setToolTip_(description)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def text(self, text, x, y, size=10):
        NSAttributedString.alloc().initWithString_attributes_(str(text), {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
        }).drawAtPoint_((x, y))

    @objc.python_method
    def line(self, points, color=None, width=1.8):
        (color or NSColor.controlAccentColor()).setStroke()
        p = NSBezierPath.bezierPath()
        p.setLineWidth_(width)
        p.moveToPoint_(points[0])
        for pt in points[1:]: p.lineToPoint_(pt)
        p.stroke()

    @objc.python_method
    def dot(self, x, y, r=3):
        NSColor.controlAccentColor().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x-r, y-r, r*2, r*2)).fill()

    @objc.python_method
    def arrow(self, x, y, up=True):
        dy = 9 if up else -9
        self.line([(x, y-dy), (x, y+dy)])
        self.line([(x-4, y+dy-(4 if up else -4)), (x, y+dy), (x+4, y+dy-(4 if up else -4))])

    @objc.python_method
    def symbol(self, name, x, y, size=18):
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image:
            image = image.imageWithSymbolConfiguration_(
                NSImageSymbolConfiguration.configurationWithPaletteColors_([NSColor.labelColor()])) or image
            image.drawInRect_(NSMakeRect(x, y, size, size))
        else:  # source-owned visible fallback; text keeps the exact semantic meaning
            self.dot(x+size/2, y+size/2, 4)

    def drawRect_(self, rect):
        if not hasattr(self, 'kind'): return
        w, h = self.bounds().size
        d = self.data
        if self.kind == 'gesture':
            g, action = d
            cy = h / 2
            self.dot(7, cy)
            if g == 'hold_swipe':
                NSColor.controlAccentColor().setStroke()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(1, cy-6, 12, 12)).stroke()
            if 'swipe' in g:
                self.line([(12, cy), (39, cy), (34, cy+4)])
            elif g == 'hold':
                NSColor.controlAccentColor().setStroke()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(1, cy-6, 12, 12)).stroke()
            else:
                self.line([(7, cy+7), (7, cy+12)])
            self.text('→', 45, cy-6, 13)
            self.symbol({'double':'cursorarrow.click.2', 'left':'cursorarrow.click',
                         'drag':'arrow.up.left.and.arrow.down.right', 'scroll':'arrow.up.arrow.down',
                         'right':'contextualmenu.and.cursorarrow', 'middle':'computermouse',
                         'space':'space', 'back':'arrow.backward', 'screen':'display.2',
                         'none':'nosign'}.get(action, 'cursorarrow'), 67, cy-9)
        elif self.kind == 'scroll':
            natural, gain, usable = d
            self.arrow(13, h/2, True)
            self.text('笔', 0, 0)
            self.text('→', 30, h/2-6, 14)
            self.arrow(58, h/2, natural)
            self.text('内容', 48, 0)
            self.text('×%g' % gain if usable else '未绑定', 80, h/2-6, 11)
        elif self.kind == 'timeline':
            ms, maximum = d
            left, right, cy = 9, w-10, h-5
            self.line([(left, cy), (right, cy)], NSColor.separatorColor())
            if ms:
                marker = left + (right-left)*min(ms/maximum, 1)
                self.line([(left, cy), (marker, cy)])
                self.dot(marker, cy, 3)
                self.text('%d ms' % ms, w/2-25, 0)
                self.text('短', left, 0); self.text('长', right-10, 0)
            else:
                self.text('关闭 · 不触发', left, 0)
        elif self.kind == 'route':
            mode, enabled = d
            self.symbol('hand.tap', 4, 8, 22)
            self.text('→', 40, 10, 16)
            self.symbol('apple.logo' if mode == 'native' else 'cursorarrow' if mode == 'legacy' else 'nosign', 71, 8, 22)
            self.text('原生手指' if mode == 'native' else '模拟鼠标' if mode == 'legacy' else '未接入手指', 108, 10, 12)
            self.text('运行' if enabled else '暂停 / 等待', w-78, 10, 11)
        elif self.kind == 'state':
            on, name = d
            self.symbol(name if on else 'pause.circle', 2, h/2-9)
            self.text('已开启' if on else '已关闭', 28, h/2-7, 11)
        elif self.kind == 'monitors':
            infos, resolved = d
            if not infos:
                self.symbol('display.trianglebadge.exclamationmark', 5, 30, 30)
                self.text('未读取到在线显示器', 50, 40, 12)
                return
            # Diagram is proportional to the live global bounds, not invented screens.
            for display_id, (x, y, bw, bh) in monitor_regions(infos, w, h):
                selected = display_id == resolved
                color = NSColor.controlAccentColor() if selected else NSColor.secondaryLabelColor()
                color.setStroke()
                box = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(NSMakeRect(x,y,bw,bh), 3, 3)
                box.setLineWidth_(2.5 if selected else 1); box.stroke()
                if selected:
                    color.colorWithAlphaComponent_(0.12).setFill(); box.fill()
                    self.symbol('cursorarrow', x+bw/2-8, y+bh/2-8, 16)
                self.text(str(display_id), x+5, y+4, 9)
            self.text('高亮 = 引擎当前光标基准屏 · 示意比例', 10, 3, 10)
