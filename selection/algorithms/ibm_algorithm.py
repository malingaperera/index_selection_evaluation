from ..selection_algorithm import SelectionAlgorithm
from ..what_if_index_creation import WhatIfIndexCreation
from ..index import Index

import itertools
import logging
import random
import time

# Maxiumum number of columns per index, storage budget in MB,
# time to "try variations" in seconds (see IBM paper),
# maximum index candidates removed while try_variations
DEFAULT_PARAMETERS = {
    'max_index_columns': 3,
    'budget': 500,
    'try_variation_seconds_limit': 10,
    'try_variation_maximum_remove': 4
}


class IndexBenefit():
    def __init__(self, index, benefit):
        self.index = index
        self.benefit = benefit

    def __eq__(self, other):
        if not isinstance(other, IndexBenefit):
            return False

        return other.index == self.index and self.benefit == other.benefit

    def __hash__(self):
        return hash((self.index, self.benefit))

    def __repr__(self):
        return f'IndexBenefit({self.index}, {self.benefit})'

    def size(self):
        return self.index.estimated_size

    def benefit_size_ratio(self):
        return self.benefit / self.size()


class IBMAlgorithm(SelectionAlgorithm):
    def __init__(self, database_connector, parameters=None):
        if parameters == None:
            parameters = {}
        SelectionAlgorithm.__init__(self, database_connector, parameters,
                                    DEFAULT_PARAMETERS)
        self.what_if = WhatIfIndexCreation(database_connector)
        # convert MB to bytes
        self.disk_constraint = self.parameters['budget'] * 1000000
        self.seconds_limit = self.parameters['try_variation_seconds_limit']
        self.maximum_remove = self.parameters['try_variation_maximum_remove']

    def _calculate_best_indexes(self, workload):
        logging.info('Calculating best indexes IBM')
        query_results, candidates = self._exploit_virtual_indexes(workload)
        index_benefits = self._calculate_index_benefits(
            candidates, query_results)
        index_benefits_subsumed = self._combine_subsumed(index_benefits)

        selected_index_benefits = set()
        disk_usage = 0
        for index_benefit in index_benefits_subsumed:
            if disk_usage + index_benefit.size() <= self.disk_constraint:
                selected_index_benefits.add(index_benefit)
                disk_usage += index_benefit.size()

        self._try_variations(selected_index_benefits, index_benefits_subsumed,
                             workload)
        return [x.index for x in selected_index_benefits]

    def _exploit_virtual_indexes(self, workload):
        query_results, index_candidates = {}, set()
        for query in workload.queries:
            plan = self.database_connector.get_plan(query)
            cost_without_indexes = plan['Total Cost']
            indexes, cost_with_recommended_indexes = self._recommended_indexes(
                query)
            query_results[query] = {
                'cost_without_indexes': cost_without_indexes,
                'cost_with_recommended_indexes': cost_with_recommended_indexes,
                'recommended_indexes': indexes
            }
            index_candidates.update(indexes)
        return query_results, index_candidates

    def _recommended_indexes(self, query):
        logging.debug('Simulating indexes')

        indexes = self._possible_indexes(query)
        for index in indexes:
            self.what_if.simulate_index(index, store_size=True)

        plan = self.database_connector.get_plan(query)
        plan_string = str(plan)
        cost = plan['Total Cost']

        self.what_if.drop_all_simulated_indexes()

        recommended_indexes = []
        for index in indexes:
            if index.hypopg_name in plan_string:
                recommended_indexes.append(index)

        logging.debug(f'Recommended indexes found: {len(recommended_indexes)}')
        return recommended_indexes, cost

    def _possible_indexes(self, query):
        # "SAEFIS" or "BFI" see IBM paper
        # This implementation is "BFI"
        columns = query.columns
        logging.debug(f'\n{query}')
        logging.debug(f'indexable columns: {len(columns)}')
        max_columns = self.parameters['max_index_columns']

        indexable_columns_per_table = {}
        for column in columns:
            if column.table not in indexable_columns_per_table:
                indexable_columns_per_table[column.table] = set()

            indexable_columns_per_table[column.table].add(column)

        possible_indexes = set()
        for table in indexable_columns_per_table:
            columns = indexable_columns_per_table[table]
            for index_length in range(1, max_columns + 1):
                possible_indexes.update(
                    set(itertools.permutations(columns, index_length)))

        logging.debug(f'possible indexes: {len(possible_indexes)}')
        return [Index(p) for p in possible_indexes]

    def _calculate_index_benefits(self, candidates, query_results):
        indexes_benefit = []

        for index_candidate in candidates:
            benefit = 0

            for query, value in query_results.items():
                if index_candidate not in value['recommended_indexes']:
                    continue
                # TODO adjust when having weights for queries
                benefit_for_query = value['cost_without_indexes'] - value[
                    'cost_with_recommended_indexes']
                benefit += benefit_for_query

            indexes_benefit.append(IndexBenefit(index_candidate, benefit))
        return sorted(indexes_benefit,
                      reverse=True,
                      key=lambda x: x.benefit_size_ratio())

    # From the paper: "Combine any index subsumed
    # by an index with a higher ratio with that index."
    # The input must be a sorted list of IndexBenefit objects.
    # E.g., the output of _calculate_index_benefits()
    def _combine_subsumed(self, index_benefits):
        combined_index_benefits = set()

        # There is no point in subsuming with less than two elements
        if len(index_benefits) < 2:
            return set(index_benefits)

        assert index_benefits[0].benefit_size_ratio(
        ) >= index_benefits[1].benefit_size_ratio(
        ), "_combine_subsumed got probably unsorted input"

        index_benefits_to_remove = set()
        for high_ratio_pos, index_benefit_high_ratio in enumerate(
                index_benefits):
            if index_benefit_high_ratio in index_benefits_to_remove:
                continue

            # We take the next element because there is no point in self comparison
            for index_benefit_lower_ratio in index_benefits[high_ratio_pos +
                                                            1:]:
                if index_benefit_lower_ratio in index_benefits_to_remove:
                    continue
                if index_benefit_high_ratio.index.subsumes(
                        index_benefit_lower_ratio.index):
                    index_benefits_to_remove.add(index_benefit_lower_ratio)

        return set(index_benefits) - index_benefits_to_remove

    def _try_variations_old(self, selected_index_benefits, index_benefits,
                            disk_usage, workload):
        logging.debug(f'Try variation for {self.seconds_limit} seconds')
        start_time = time.time()

        not_used_indexes = [
            x for x in index_benefits if x not in selected_index_benefits
        ]
        current_cost = self._evaluate_workload(selected_index_benefits, [],
                                               workload)
        logging.debug(f'Initial cost \t{current_cost}')

        while start_time + self.seconds_limit > time.time():
            disk_usage = sum([x.size() for x in selected_index_benefits])
            # randomly choose indexes from current index set
            number_removed = random.randrange(
                1, self.maximum_remove) if self.maximum_remove > 1 else 1
            remove_at_indexes = list(range(len(selected_index_benefits)))
            random.shuffle(remove_at_indexes)
            remove_at_indexes = remove_at_indexes[:number_removed]

            # remove these chosen indexes
            removed = []
            for remove_at_index in sorted(remove_at_indexes, reverse=True):
                index = selected_index_benefits[remove_at_index]
                disk_usage -= index.size()
                del selected_index_benefits[remove_at_index]
                removed.append(index)

            # adding random unused indexes
            new_selected = []
            for i in range(number_removed):
                maximum_size = self.disk_constraint - disk_usage
                candidates = [
                    x for x in not_used_indexes if x.size() <= maximum_size
                ]
                if len(candidates) == 0:
                    break
                random.shuffle(candidates)
                selected_index = candidates[0]
                disk_usage += selected_index.size()
                new_selected.append(selected_index)
                not_used_indexes.remove(selected_index)
            # reevaluate new selected and replace if lower cost
            cost = self._evaluate_workload(selected_index_benefits,
                                           new_selected, workload)
            if cost < current_cost:
                not_used_indexes.extend(removed)
                selected_index_benefits.extend(new_selected)
                current_cost = cost
                logging.debug(f'Lower cost found \t{current_cost}')
            else:
                selected_index_benefits.extend(removed)
                not_used_indexes.extend(new_selected)

        return selected_index_benefits

    def _try_variations(self, selected_index_benefits, index_benefits,
                        workload):
        logging.debug(f'Try variation for {self.seconds_limit} seconds')
        start_time = time.time()

        not_used_index_benefits = [
            x for x in index_benefits if x not in selected_index_benefits
        ]

        min_length = min(len(selected_index_benefits), len(not_used_index_benefits))
        if self.maximum_remove > min_length:
            self.maximum_remove = min_length

        if self.maximum_remove == 0:
            return selected_index_benefits

        current_cost = self._evaluate_workload(selected_index_benefits,
                                               workload)
        logging.debug(f'Initial cost \t{current_cost}')

        while start_time + self.seconds_limit > time.time():
            number_of_exchanges = random.randrange(
                1, self.maximum_remove) if self.maximum_remove > 1 else 1
            indexes_to_remove = frozenset(
                random.sample(selected_index_benefits, k=number_of_exchanges))

            new_variaton = set(selected_index_benefits - indexes_to_remove)
            new_variation_size = sum([x.size() for x in new_variaton])

            indexes_to_add = random.sample(not_used_index_benefits,
                                           k=number_of_exchanges)
            assert len(indexes_to_add) == len(indexes_to_remove),
            '_try_variations must remove the same number of indexes that are added.'
            for index_benefit in indexes_to_add:
                if index_benefit.size(
                ) + new_variation_size > self.disk_constraint:
                    continue
                new_variaton.add(index_benefit)
                new_variation_size += index_benefit.size()

            cost_of_variation = self._evaluate_workload(new_variaton, workload)

            if cost_of_variation < current_cost:
                logging.debug(f'Lower cost found \t{current_cost}')
                current_cost = cost_of_variation
                selected_index_benefits = new_variaton

        return selected_index_benefits

    def _evaluate_workload(self, index_benefits, workload):
        index_candidates = [
            index_benefit.index for index_benefit in index_benefits
        ]
        return self.cost_evaluation.calculate_cost(workload, index_candidates)
