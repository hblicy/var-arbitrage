const routeIsEnabled = (opportunity, enabledExchanges) => {
  const buyExchange = opportunity.details?.buy_exchange?.toLowerCase();
  const sellExchange = opportunity.details?.sell_exchange?.toLowerCase();
  return enabledExchanges.has(buyExchange) && enabledExchanges.has(sellExchange);
};

const OBSERVATION_PREVIEW_LIMIT = 20;

export const getObservationDisplay = (opportunities, showAll) => {
  const visible = showAll
    ? opportunities
    : opportunities.slice(0, OBSERVATION_PREVIEW_LIMIT);

  return {
    visible,
    hiddenCount: opportunities.length - visible.length,
  };
};

export const partitionOpportunities = (opportunities, enabledExchanges) => {
  const executable = [];
  const observation = [];

  for (const opportunity of opportunities) {
    if (!routeIsEnabled(opportunity, enabledExchanges)) continue;
    if (opportunity.details?.entry_check_supported === true) {
      executable.push(opportunity);
    } else {
      observation.push(opportunity);
    }
  }

  return { executable, observation };
};
