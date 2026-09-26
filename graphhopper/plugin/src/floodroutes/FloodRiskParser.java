package floodroutes;

import com.graphhopper.reader.ReaderWay;
import com.graphhopper.routing.ev.EdgeIntAccess;
import com.graphhopper.routing.ev.IntEncodedValue;
import com.graphhopper.routing.util.parsers.TagParser;
import com.graphhopper.storage.IntsRef;
import com.graphhopper.util.PointList;

/**
 * Sets flood_risk per edge from the edge's own geometry (the "point_list" tag
 * OSMReader attaches for each edge, as the curvature parser uses). This is the
 * same edge-intersects-polygon test custom-model areas make at query time,
 * done once at import.
 */
public class FloodRiskParser implements TagParser {
    private final IntEncodedValue enc;
    private final FloodRiskIndex index;

    public FloodRiskParser(IntEncodedValue enc, FloodRiskIndex index) {
        this.enc = enc;
        this.index = index;
    }

    @Override
    public void handleWayTags(int edgeId, EdgeIntAccess edgeIntAccess, ReaderWay way, IntsRef relationFlags) {
        PointList pl = way.getTag("point_list", null);
        if (pl == null || pl.size() < 2)
            return;
        int tier = index.maxTier(pl);
        if (tier > 0)
            enc.setInt(false, edgeId, edgeIntAccess, tier);
    }
}
