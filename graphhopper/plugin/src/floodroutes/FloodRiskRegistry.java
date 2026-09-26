package floodroutes;

import com.graphhopper.routing.ev.DefaultImportRegistry;
import com.graphhopper.routing.ev.ImportRegistry;
import com.graphhopper.routing.ev.ImportUnit;
import com.graphhopper.routing.ev.IntEncodedValueImpl;

/**
 * Adds the "flood_risk" encoded value (0 none, 1 low, 2 medium, 3 high) to
 * GraphHopper's default import units. It is a stock IntEncodedValueImpl, so an
 * unmodified GraphHopper server can load the resulting graph.
 */
public class FloodRiskRegistry implements ImportRegistry {
    public static final String NAME = "flood_risk";
    private final DefaultImportRegistry defaults = new DefaultImportRegistry();
    private final FloodRiskIndex index;

    public FloodRiskRegistry(FloodRiskIndex index) {
        this.index = index;
    }

    @Override
    public ImportUnit createImportUnit(String name) {
        if (NAME.equals(name))
            return ImportUnit.create(NAME,
                    props -> new IntEncodedValueImpl(NAME, 2, false),
                    (lookup, props) -> new FloodRiskParser(lookup.getIntEncodedValue(NAME), index));
        return defaults.createImportUnit(name);
    }
}
