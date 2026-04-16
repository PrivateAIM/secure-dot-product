from typing import Any, Optional

from flame.proxy import ProxyModel, ProxyAnalyzer, Proxy, ProxyAggregator


class MyAnalyzer(ProxyAnalyzer):
    proxy_params: Optional[dict[str, tuple[float, float]]] = None

    def __init__(self, flame):
        super().__init__(flame)  # Connects this analyzer to the FLAME components

    def analysis_method(self, data, aggregator_results) -> Any:
        if self.num_iterations == 1:
            self.proxy_params = self.flame.await_intermediate_data(
                senders=[self.proxy_id], message_category="proxy_params"
            )[self.proxy_id]
        if self.num_iterations > 0:
            # TODO: Extract a and b from data=list_of_datasources[{query: raw_data}]
            #  (keep in mind that s3 returns files in binary format, thus use ``.decode('utf8')`` for text files)
            a, b = data[0].values().decode("utf8")

            for partner_node, shared_secrets in self.proxy_params.items():
                # Extract shared secrets
                x, y = shared_secrets

                # TODO: calc c_n using proxy_params={partner_node_id: (x_n, y_n)}
                d = a - x
                e = b - y
                z = x * y
                c_n = d + e + d * e + z

                # Send c value to partner
                self.flame.send_intermediate_data(receivers=[partner_node], data=c_n, message_category="data_share")
            # Await c values by all partners
            data_dict = self.flame.await_intermediate_data(
                senders=list(self.proxy_params.keys()), message_category="data_share"
            )

            # Calculate analysis result
            analysis_result = 0
            for sender, shared_data in data_dict.items():
                # TODO: Do something with the data shared by other analyzers
                analysis_result += shared_data

            # Send analysis result to aggregator
            self.flame.send_intermediate_data(
                receivers=[self.flame.aggregator_id], data=analysis_result, message_category="analysis_results"
            )
        return None


class MyProxy(Proxy):
    def __init__(self, flame):
        super().__init__(flame)  # Connects this proxy to the FLAME components

    def proxy_aggregation_method(self, analysis_results: list[Any]) -> Any:
        # Calculate and share analyzer secrets in iteration 0
        if self.num_iterations == 0:
            node_specific_proxy_params = self.calc_analyzer_pair_params()

            # Distribute shared secrets
            for node_id in node_specific_proxy_params.keys():
                self.flame.send_intermediate_data(
                    receivers=[node_id], data=node_specific_proxy_params[node_id], message_category="proxy_params"
                )
        return None

    def calc_analyzer_pair_params(self) -> dict[str, dict[str, tuple[float, float]]]:
        # Calculate pairwise secrets for each unique node pair
        node_specific_proxy_params = {}
        for i, node_n in enumerate(self.analyzer_ids):
            for j, node_m in enumerate(self.analyzer_ids[i:]):
                if node_n != node_m:
                    if node_n not in node_specific_proxy_params.keys():
                        node_specific_proxy_params[node_n] = {}
                    if node_m not in node_specific_proxy_params.keys():
                        node_specific_proxy_params[node_m] = {}

                    # TODO: calc x_n, x_m, y_n, y_m for node pair (node_i X node_j)
                    x_n, x_m, y_n, y_m = 0, 1, 2, 3

                    # Save shared secrets in dict (first layer: secret holder, second layer: secret partner
                    node_specific_proxy_params[node_n][node_m] = (x_n, y_n)
                    node_specific_proxy_params[node_m][node_n] = (x_m, y_m)

        return node_specific_proxy_params


class MyAggregator(ProxyAggregator):
    def __init__(self, flame):
        super().__init__(flame)  # Connects this aggregator to the FLAME components

    def aggregation_method(self, analysis_results):
        if self.num_iterations == 0:
            return None
        else:
            # Await and receive list of analyzer results
            list_of_analysis_results = list(
                self.flame.await_intermediate_data(
                    senders=self.analyzer_ids, message_category="analysis_results"
                ).values()
            )
            # TODO: calc final/aggregate individual results
            final = sum(list_of_analysis_results)

            return final

    def has_converged(self, result, last_result):
        # TODO: define convergence criteria
        conv_criteria = True

        return conv_criteria


def main():
    ProxyModel(
        analyzer=MyAnalyzer,  # Custom analyzer class
        proxy=MyProxy,  # Custom proxy class
        aggregator=MyAggregator,  # Custom aggregator class
        data_type="s3",  # Type of data source ('fhir' or 's3')
        query="",  # Query or list of queries to retrieve data
        num_proxy_nodes=1,  # Number of proxy nodes partaking in this analysis
        simple_analysis=False,  # True for single-iteration; False for multi-iterative analysis
        output_type="str",  # Output format for the final result ('str', 'bytes', or 'pickle')
        multiple_results=False,  # Can be set to True to return highest iterable-level of results as separate files
        analyzer_kwargs=None,  # Additional keyword arguments for the custom analyzer constructor (i.e. MyAnalyzer)
        proxy_kwargs=None,  # Additional keyword arguments for the custom proxy constructor (i.e. MyProxy)
        aggregator_kwargs=None,  # Additional keyword arguments for the custom aggregator constructor (i.e. MyAggregator)
    )


if __name__ == "__main__":
    main()
