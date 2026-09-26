package floodroutes;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.graphhopper.util.PointList;
import org.locationtech.jts.geom.*;
import org.locationtech.jts.geom.prep.PreparedGeometry;
import org.locationtech.jts.geom.prep.PreparedGeometryFactory;
import org.locationtech.jts.index.strtree.STRtree;

import java.io.File;
import java.io.IOException;
import java.util.List;
import java.util.Map;

/** Risk-tier polygons from risk/flood_risk_tiers.geojson in an STRtree. */
public class FloodRiskIndex {
    private static final Map<String, Integer> TIERS = Map.of("low", 1, "medium", 2, "high", 3);
    private final GeometryFactory gf = new GeometryFactory();
    private final STRtree tree = new STRtree();
    private final int[] counts = new int[4];
    private long edges, hits;

    record Entry(PreparedGeometry geom, int tier) {}

    public FloodRiskIndex(File geojson) throws IOException {
        JsonNode fc = new ObjectMapper().readTree(geojson);
        for (JsonNode f : fc.get("features")) {
            Integer tier = TIERS.get(f.get("properties").get("tier").asText());
            if (tier == null)
                continue;
            JsonNode g = f.get("geometry");
            Geometry geom = switch (g.get("type").asText()) {
                case "Polygon" -> polygon(g.get("coordinates"));
                case "MultiPolygon" -> {
                    JsonNode parts = g.get("coordinates");
                    Polygon[] ps = new Polygon[parts.size()];
                    for (int i = 0; i < ps.length; i++) ps[i] = polygon(parts.get(i));
                    yield gf.createMultiPolygon(ps);
                }
                default -> throw new IOException("unsupported geometry " + g.get("type"));
            };
            tree.insert(geom.getEnvelopeInternal(), new Entry(PreparedGeometryFactory.prepare(geom), tier));
            counts[tier]++;
        }
        tree.build();
    }

    private Polygon polygon(JsonNode rings) {
        LinearRing shell = ring(rings.get(0));
        LinearRing[] holes = new LinearRing[rings.size() - 1];
        for (int i = 1; i < rings.size(); i++) holes[i - 1] = ring(rings.get(i));
        return gf.createPolygon(shell, holes);
    }

    private LinearRing ring(JsonNode pts) {
        Coordinate[] cs = new Coordinate[pts.size()];
        for (int i = 0; i < cs.length; i++) cs[i] = new Coordinate(pts.get(i).get(0).asDouble(), pts.get(i).get(1).asDouble());
        return gf.createLinearRing(cs);
    }

    /** Highest tier whose polygon intersects the edge geometry, 0 if none. */
    @SuppressWarnings("unchecked")
    public synchronized int maxTier(PointList pl) {
        edges++;
        Coordinate[] cs = new Coordinate[pl.size()];
        for (int i = 0; i < cs.length; i++) cs[i] = new Coordinate(pl.getLon(i), pl.getLat(i));
        LineString line = gf.createLineString(cs);
        int best = 0;
        for (Entry e : (List<Entry>) tree.query(line.getEnvelopeInternal())) {
            if (e.tier() > best && e.geom().intersects(line)) {
                best = e.tier();
                if (best == 3) break;
            }
        }
        if (best > 0) hits++;
        return best;
    }

    public String summary() {
        return String.format("%d polygons (high %d, medium %d, low %d); %,d edges checked, %,d in a risk area",
                counts[1] + counts[2] + counts[3], counts[3], counts[2], counts[1], edges, hits);
    }
}
