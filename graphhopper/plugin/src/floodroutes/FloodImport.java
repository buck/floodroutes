package floodroutes;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.yaml.YAMLFactory;
import com.graphhopper.GraphHopper;
import com.graphhopper.GraphHopperConfig;
import io.dropwizard.jackson.Jackson;

import java.io.File;

/**
 * GraphHopper import with the flood_risk encoded value.
 * Same as `java -jar graphhopper-web.jar import config.yml` (GraphHopperManaged
 * does new GraphHopper().init(config)) plus our ImportRegistry.
 *
 * Usage: java -cp graphhopper-web-11.0.jar:floodroutes-gh.jar floodroutes.FloodImport config.yml risk.geojson
 */
public class FloodImport {
    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            System.err.println("usage: FloodImport <config.yml> <flood_risk_tiers.geojson>");
            System.exit(2);
        }
        JsonNode root = new ObjectMapper(new YAMLFactory()).readTree(new File(args[0]));
        GraphHopperConfig config = Jackson.newObjectMapper().treeToValue(root.get("graphhopper"), GraphHopperConfig.class);
        FloodRiskIndex index = new FloodRiskIndex(new File(args[1]));
        GraphHopper hopper = new GraphHopper();
        hopper.setImportRegistry(new FloodRiskRegistry(index));
        hopper.init(config);
        hopper.importAndClose();
        System.out.println("flood_risk: " + index.summary());
    }
}
