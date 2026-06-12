// Tree-shaken ECharts setup + Ayu dark theme tokens.
import * as echarts from "echarts/core";
import { BarChart, LineChart } from "echarts/charts";
import {
  DataZoomComponent, GridComponent, LegendComponent,
  MarkAreaComponent, TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent,
  DataZoomComponent, MarkAreaComponent, CanvasRenderer,
]);

export const C = {
  bg: "transparent", ink: "#bfbdb6", dim: "#565b66", line: "#1c212e",
  amber: "#ffb454", cyan: "#59c2ff", coral: "#ff6b6b", green: "#aad94c",
};

const FONT = { fontFamily: "'IBM Plex Mono', monospace", color: C.dim, fontSize: 10 };

// echarts keeps every init()'d instance in a module registry; without disposal
// they survive innerHTML replacement. The dashboard re-renders every 60s, so a
// tab left open all day would accumulate hundreds of canvases. Track live
// instances + their observers and tear them down before each render.
const live: { chart: echarts.ECharts; ro: ResizeObserver }[] = [];

export function disposeCharts(): void {
  for (const { chart, ro } of live) {
    ro.disconnect();
    chart.dispose();
  }
  live.length = 0;
}

export function mountChart(el: HTMLElement): echarts.ECharts {
  const chart = echarts.init(el, undefined, { renderer: "canvas" });
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(el);
  live.push({ chart, ro });
  return chart;
}

export function sparkOption(values: [string, number][]): echarts.EChartsCoreOption {
  return {
    backgroundColor: C.bg,
    grid: { left: 2, right: 2, top: 4, bottom: 2 },
    xAxis: { type: "time", show: false },
    yAxis: { type: "value", show: false, min: -1, max: 1 },
    series: [{
      type: "line", data: values, showSymbol: false, smooth: 0.3,
      lineStyle: { width: 1.5, color: C.cyan },
      areaStyle: { opacity: 0.12, color: C.cyan },
    }],
    animation: false,
  };
}

export function trendOption(
  sentiment: [string, number][],
  volume: [string, number][],
  alertWindows: { start: string; end: string; high: boolean }[],
): echarts.EChartsCoreOption {
  return {
    backgroundColor: C.bg,
    grid: { left: 46, right: 46, top: 30, bottom: 56 },
    legend: {
      top: 0, textStyle: { ...FONT, color: C.ink },
      itemWidth: 14, itemHeight: 8,
      data: ["情緒", "聲量"],
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#131721", borderColor: C.line,
      textStyle: { ...FONT, color: C.ink, fontSize: 11 },
    },
    xAxis: {
      type: "time",
      axisLine: { lineStyle: { color: C.line } },
      axisLabel: FONT, splitLine: { show: false },
    },
    yAxis: [
      {
        type: "value", min: -1, max: 1, name: "sent",
        nameTextStyle: FONT, axisLabel: FONT,
        splitLine: { lineStyle: { color: C.line } },
      },
      {
        type: "value", name: "vol", nameTextStyle: FONT, axisLabel: FONT,
        splitLine: { show: false },
      },
    ],
    dataZoom: [{ type: "inside" }, {
      type: "slider", height: 16, bottom: 8,
      borderColor: C.line, backgroundColor: "#11151f",
      fillerColor: "rgba(89,194,255,0.10)", textStyle: FONT,
      handleStyle: { color: C.dim },
    }],
    series: [
      {
        name: "情緒", type: "line", data: sentiment, showSymbol: false,
        smooth: 0.25, lineStyle: { width: 2, color: C.cyan }, z: 3,
        markArea: {
          silent: true,
          itemStyle: { color: "rgba(255,107,107,0.10)" },
          data: alertWindows.map((w) => [
            { xAxis: w.start, itemStyle: w.high ? { color: "rgba(255,107,107,0.16)" } : { color: "rgba(255,180,84,0.10)" } },
            { xAxis: w.end },
          ]),
        },
      },
      {
        name: "聲量", type: "bar", yAxisIndex: 1, data: volume,
        itemStyle: { color: "rgba(255,180,84,0.55)", borderRadius: [2, 2, 0, 0] },
        barMaxWidth: 10, z: 2,
      },
    ],
  };
}
